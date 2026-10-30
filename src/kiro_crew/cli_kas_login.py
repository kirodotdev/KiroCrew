"""``kirocrew kas login | whoami | logout | route`` -- Crew's own Kiro sign-in from a terminal.

The dashboard's Kiro sign-in card drives :class:`kiro_crew.auth.service.KasLoginService`
over HTTP. These verbs drive the same service from a shell, which is what a machine
with no browser and no dashboard session needs: the remote crew ``kirocrew cloud
login`` signs in over SSM, and any headless host an operator reaches by ssh. They
exist because on the KAS bundle route (``agent.kas_bundle`` on) the vault is the
engine's ONLY credential and kiro-cli is not installed, so ``kiro-cli login`` /
``whoami`` / ``logout`` have nothing to act on there.

The verbs speak the vocabulary the cloud sign-in already reads from kiro-cli:

- ``login`` prints the verification URL and the ``code: XXXX-XXXX`` line, then keeps
  polling until the user approves in a browser on any device (the device-code flow,
  the one transport that needs no callback to reach this machine), and exits 0
  once the credential is in the vault. On a vault that already holds a usable
  identity it prints ``already signed in`` and exits 0, as ``kiro-cli login`` does
  over a live session, so switching accounts is ``logout`` then ``login``.
- ``whoami`` exits 0 and prints the identity when signed in, exits 1 when not;
  ``--format json`` prints the ``kiro-cli whoami --format json`` document
  (:func:`kiro_crew.auth.whoami.whoami_document`).
- ``logout`` removes every stored identity. A gateway on this host notices on its
  next turn: the vault is part of its identity fingerprint, and the pre-turn
  identity-change check retires the running agent processes, so nothing keeps
  serving the signed-out account past that turn.
- ``route`` prints which program owns the Kiro sign-in on this host --
  ``kas-bundle`` when the next spawn runs Crew's own engine, ``kiro-cli`` otherwise,
  and a plain-language error with exit 1 when the config cannot be read (the owner
  is then unknown, not kiro-cli). The cloud sign-in scripts read it on the box to
  pick the sign-in program; it is the answer to "how does a remote command know
  the remote config".

Operator-facing like ``kirocrew kas install``: it runs outside the agent sandbox,
the only place the ``kas`` vault leaf is writable. Not an LLM-facing command and
deliberately not an MCP tool: a sign-in moves a credential, and the agent must
never be able to choose whose.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

#: What ``kirocrew kas route`` prints when Crew's own engine runs the agents and the
#: vault is the only sign-in. The cloud sign-in's remote scripts compare against this
#: exact spelling (``cloud/login_vault.ROUTE_KAS_BUNDLE`` carries the same constant on
#: the reading side; ``test_cli_kas_login.py`` pins the two equal).
ROUTE_KAS_BUNDLE = "kas-bundle"
#: Printed for every other configuration: the kiro-cli backend, the KAS relay route
#: and any harness that signs in through kiro-cli. It names the program that owns the
#: sign-in, so the cloud scripts keep running ``kiro-cli`` exactly as before.
ROUTE_KIRO_CLI = "kiro-cli"
#: ``kas route``'s exit status when the config could not be read, so the owner is
#: UNKNOWN. Distinct from the shell's 127 (no ``kirocrew`` on the box) and argparse's
#: 2 (a ``kirocrew`` too old to have the verb), which the cloud scripts read as "no
#: route answer, kiro-cli as always"; this one they read as "do not touch either
#: program" (``cloud/login_vault.route_prelude``). A Python crash exits 1 too, and
#: is rightly read the same way: an answer that cannot be trusted is not kiro-cli.
ROUTE_UNKNOWN_EXIT = 1

#: Seconds between two polls of a pending device authorization. kiro-cli's own
#: device flow polls at the issuer's suggested interval, which is five seconds for
#: both SSO-OIDC and the Kiro social service; one constant here keeps the CLI inside
#: both issuers' ``slow_down`` thresholds.
POLL_INTERVAL_SECS = 5.0

_PROVIDERS = ("builder_id", "idc", "google", "github")


def add_kas_login_parsers(sub: Any) -> None:
    """Attach ``login`` / ``whoami`` / ``logout`` / ``route`` to the ``kas`` subparsers."""
    login = sub.add_parser(
        "login",
        help="Sign Kiro Crew's own vault in with a device code (no browser needed here)",
    )
    login.add_argument(
        "--provider",
        default="builder_id",
        choices=_PROVIDERS,
        help="Which identity to sign in with (default: builder_id)",
    )
    login.add_argument(
        "--start-url",
        default="",
        help="IAM Identity Center start URL (required with --provider idc)",
    )
    login.add_argument(
        "--region",
        default="",
        help="IAM Identity Center region (default: the Builder ID region)",
    )
    whoami = sub.add_parser("whoami", help="Show which Kiro identity the vault is signed in as")
    whoami.add_argument("--format", default="text", choices=("text", "json"), help="Output format")
    sub.add_parser("logout", help="Remove every Kiro identity from Kiro Crew's vault")
    sub.add_parser(
        "route",
        help=f"Print which program owns the Kiro sign-in here: {ROUTE_KAS_BUNDLE} or {ROUTE_KIRO_CLI}",
    )


def _route(_args: argparse.Namespace) -> int:
    from kiro_crew.auth.whoami import (
        ConfigUnreadable,
        agent_owns_sign_in,
        load_sign_in_owner_config,
    )
    from kiro_crew.config.paths import data_home

    # Not ``vault_owns_sign_in``: that answers "kiro-cli" for a config it cannot
    # read, the right fallback for an in-process caller but, printed from this
    # verb, it would send a bundle-route box down a kiro-cli branch it cannot run
    # (or, with kiro-cli installed, sign the wrong store out). A config that
    # cannot be read -- raised, or read only as defaults -- is reported with the
    # path and the fault, exit 1, so the remote scripts know the owner is UNKNOWN.
    try:
        cfg = load_sign_in_owner_config()
    except ConfigUnreadable as exc:
        print(
            f"❌ could not read the Kiro Crew config under {data_home()}: {exc}. Which program "
            "owns the Kiro sign-in here is unknown; fix the config, then run this again.",
            file=sys.stderr,
        )
        return ROUTE_UNKNOWN_EXIT
    print(ROUTE_KAS_BUNDLE if agent_owns_sign_in(cfg.agent) else ROUTE_KIRO_CLI)
    return 0


def _whoami(args: argparse.Namespace) -> int:
    from kiro_crew.auth.bridge import default_token_store, describe_vault_identity
    from kiro_crew.auth.whoami import resolve_signed_in, whoami_document

    token = resolve_signed_in(default_token_store())
    if token is None:
        print("Not signed in to Kiro Crew.", file=sys.stderr)
        return 1
    if args.format == "json":
        print(json.dumps(whoami_document(token)))
        return 0
    doc = whoami_document(token)
    print(f"Signed in to Kiro Crew: {doc['accountType']} ({describe_vault_identity()})")
    if doc.get("startUrl"):
        print(f"  start URL: {doc['startUrl']}")
    return 0


def _logout(_args: argparse.Namespace) -> int:
    from kiro_crew.auth.bridge import default_token_store
    from kiro_crew.auth.service import KasLoginService
    from kiro_crew.auth.store import KNOWN_IDENTITIES, TokenStoreError

    store = default_token_store()
    token = store.resolve()
    # The service's sign-out runs even over an empty vault: besides the deletes it
    # advances the store's sign-out generation, which is what stops a dashboard
    # login pending in the gateway at this moment from filling the vault once its
    # code is approved. An empty vault has nothing to delete, but that login still
    # has to be refused.
    identity = token.identity if token is not None else KNOWN_IDENTITIES[0]
    try:
        asyncio.run(KasLoginService(store).logout(identity))
    except TokenStoreError as exc:
        print(f"❌ sign-out did not complete: {exc}", file=sys.stderr)
        return 1
    if token is None:
        print("Not signed in to Kiro Crew; nothing to sign out.")
    else:
        print("Signed out of Kiro Crew.")
    return 0


def _login(args: argparse.Namespace) -> int:
    from kiro_crew.auth.bridge import default_token_store, describe_vault_identity
    from kiro_crew.auth.whoami import resolve_signed_in

    store = default_token_store()
    if resolve_signed_in(store) is not None:
        # The same answer kiro-cli gives over a live session, in the words the
        # cloud sign-in's parser reads as "already signed in": switching accounts
        # is `kirocrew kas logout` first, never a second identity stacked on top.
        print(
            f"Already signed in to Kiro Crew ({describe_vault_identity()}). "
            "Run `kirocrew kas logout` first to switch accounts."
        )
        return 0
    if args.provider == "idc" and not (args.start_url or "").strip():
        print("❌ --provider idc requires --start-url", file=sys.stderr)
        return 2
    # A lapsed entry in another slot would outrank a fresh lower-priority sign-in
    # at resolve time (the store picks by fixed slot order, not recency), so it is
    # named as the slot this login replaces: the service removes every other slot
    # once the new credential has landed, never before. Read here, off any loop.
    stale = store.resolve()
    try:
        return asyncio.run(
            _run_device_login(
                store,
                provider=args.provider,
                start_url=args.start_url,
                region=args.region,
                replaces=stale.identity if stale is not None else "",
            )
        )
    except KeyboardInterrupt:
        print("\nSign-in cancelled; nothing was stored.", file=sys.stderr)
        return 130


async def _run_device_login(
    store: Any, *, provider: str, start_url: str, region: str, replaces: str = ""
) -> int:
    """Begin a device-code login, print its prompt, poll until it ends."""
    # Imported here, not at module scope: ``kirocrew``'s argument parser imports
    # this module while it is built, and aiohttp and the auth subsystem (with the
    # ``cryptography`` wheel behind the vault) must not be the price of `--help`.
    import aiohttp

    from kiro_crew.auth.login.builder_id import BuilderIdAuthError
    from kiro_crew.auth.login.device import DeviceAuthError
    from kiro_crew.auth.service import (
        InvalidRegionError,
        KasLoginService,
        MissingStartUrlError,
        SignedOutDuringLoginError,
        UnknownLoginError,
    )

    service = KasLoginService(store)
    try:
        try:
            begun = await service.begin_device(
                provider,
                start_url=start_url,
                region=region,
                replaces=replaces,
            )
        except MissingStartUrlError:
            print("❌ --provider idc requires --start-url", file=sys.stderr)
            return 2
        except InvalidRegionError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        except (BuilderIdAuthError, DeviceAuthError) as exc:
            print(f"❌ the sign-in service refused to start a sign-in: {exc}", file=sys.stderr)
            return 1
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            print(f"❌ could not reach the sign-in service: {exc}", file=sys.stderr)
            return 1
        # Line-buffered on purpose: the cloud sign-in reads these two lines out of
        # a log this process writes under nohup, where stdout is block-buffered.
        print(f"Open {begun['verification_uri_complete']} in a browser on any device", flush=True)
        print(f"code: {begun['user_code']}", flush=True)
        # Worded so the only "code: ..." in this output is the line above: the
        # cloud sign-in's parser takes the first match.
        print(f"Waiting for approval; this prompt expires at {begun['expires_at']} ...", flush=True)
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECS)
            try:
                result = await service.poll_device(begun["login_id"])
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # The service absorbs a few dropped polls itself; past its budget
                # the outage is real, and the user gets a line, not a traceback.
                print(
                    f"❌ lost contact with the sign-in service while waiting for approval: "
                    f"{exc}; nothing was stored. Run the command again.",
                    file=sys.stderr,
                )
                return 1
            except (UnknownLoginError, SignedOutDuringLoginError):
                print(
                    "❌ this sign-in was cancelled by a sign-out before it was approved; "
                    "nothing was stored.",
                    file=sys.stderr,
                )
                return 1
            status = result.get("status")
            if status == "pending":
                continue
            if status == "authorized":
                print(
                    f"Signed in to Kiro Crew as {result.get('provider') or provider}.", flush=True
                )
                if result.get("code") == "previous_identity_not_removed":
                    print(
                        "⚠ an earlier identity could not be removed from the vault; "
                        "run `kirocrew kas logout` and sign in again if the wrong account answers",
                        file=sys.stderr,
                    )
                return 0
            if status == "expired":
                print(
                    "❌ the code expired before it was approved; run the command again.",
                    file=sys.stderr,
                )
                return 1
            code = result.get("code") or "error"
            print(f"❌ sign-in failed ({code}); nothing was stored.", file=sys.stderr)
            return 1
    finally:
        await service.close()


def kas_login_cmd(args: argparse.Namespace) -> int | None:
    """Dispatch the sign-in verbs; ``None`` when *args* names none of them."""
    action = getattr(args, "kas_action", None)
    if action == "login":
        return _login(args)
    if action == "whoami":
        return _whoami(args)
    if action == "logout":
        return _logout(args)
    if action == "route":
        return _route(args)
    return None
