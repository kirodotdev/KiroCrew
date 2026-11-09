"""``kirocrew connected-computer``: the CLI half of connected computers.

Run on the REMOTE gateway's host (the first two over SSH by the local machine):

* ``enroll --stdin`` -- read ``{"base", "fingerprint", "label"}``, enroll the
  calling computer as the SSH user, print one JSON line with its id and connector
  credential. Refused while the switch is off in this gateway's config. A
  renewal (``"renew": true``) must also carry the ``credential`` it replaces,
  which must verify in full except for its expiry
  (:func:`.credentials.verify_for_renewal`); a refusal answers a ``code``
  (``revoked``, ``removed``, ``superseded``, ...) the computer acts on.
* ``forget --stdin`` -- read ``{"device_id", "fingerprint"}`` and revoke it
  (marked revoked, credential generation bumped), as a Disconnect does.
* ``devices`` / ``revoke <id>`` / ``remove <id>`` -- the owner's own view; an open
  socket of a revoked computer ends within its re-check interval.

Run on the LOCAL machine:

* ``status`` -- this computer's grants.
* ``off`` -- revoke every grant now (works with the switch off, offline and with
  the keyring locked). The refusal and the Off generation are persisted first;
  then it waits for a running local gateway to stop the processes (bounded), and
  only then tells each remote gateway, leaving an unreachable one queued.
* ``reset-protection`` -- start over after the OS keyring lost this computer's
  key: confirmed in an interactive terminal, revokes every grant, deletes nothing.

There is no CLI verb that SHARES a folder. A terminal check and a typed name
are answered by any pseudo-terminal wrapper (``script -c``, ``expect``), so a
local process could grant itself a root. Sharing needs a native dialog and
folder picker: the desktop app's menu, or the local dashboard's consent redeemed
by the desktop shell (:mod:`.local_routes`). Every verb here only narrows,
except the remote gateway's own enrollment, which the local side starts after
that native consent.

None of these is an agent tool, by design: an agent must not enroll a computer,
grant itself a folder or lift a revocation. The read-only status is shown in the
dashboard instead.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from typing import Any, Optional

_MAX_STDIN: int = 8192


def register_parser(sub: Any) -> None:
    from kiro_crew import cli_help

    p = cli_help.add_command(sub, "connected-computer")
    cc = p.add_subparsers(dest="cc_command", metavar="<command>")
    cc.required = True
    for name, text in (
        ("enroll", "(remote gateway, over SSH) enroll the calling computer"),
        ("forget", "(remote gateway, over SSH) revoke the calling computer"),
    ):
        child = cc.add_parser(name, help=text)
        child.add_argument("--stdin", action="store_true", required=True)
    devices = cc.add_parser("devices", help="(remote gateway) list enrolled computers")
    devices.add_argument("--json", action="store_true")
    for name, text in (
        ("revoke", "(remote gateway) refuse a computer's credentials"),
        ("remove", "(remote gateway) retire a computer; its files are untouched"),
    ):
        child = cc.add_parser(name, help=text)
        child.add_argument("device_id")
    status = cc.add_parser("status", help="(this computer) list its grants")
    status.add_argument("--json", action="store_true")
    cc.add_parser("off", help="(this computer) turn off every grant now")
    cc.add_parser(
        "reset-protection",
        help="(this computer) start over after the OS keyring lost this computer's key",
    )
    cc.add_parser(
        "desktop-host",
        help="(this computer) the desktop app's local controller, on stdin/stdout only",
    )


def _print(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _reply_on_channel(answer: dict[str, Any]) -> None:
    """Send the enrollment answer, which holds the connector credential, back to the computer.

    ``enroll --stdin`` is the command the computer runs over SSH (:mod:`.enroll`),
    so this process's stdout IS the SSH channel back to it, the one place the
    credential and peer key are meant to go. They are written as bytes to that
    stream only: never through :func:`_print`, a logger or stderr, and no other
    path in this module writes a credential.
    """
    sys.stdout.flush()
    out = sys.stdout.buffer
    out.write((json.dumps(answer, separators=(",", ":")) + "\n").encode("utf-8"))
    out.flush()


def _read_stdin() -> Optional[dict]:
    raw = sys.stdin.read(_MAX_STDIN)
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _remote_enroll() -> int:
    from kiro_crew.connected_computers.credentials import (
        CredentialRejected,
        mint,
        peer_key_from_secret,
        verify_for_renewal,
    )
    from kiro_crew.connected_computers.devices import DeviceRegistry, EnrollmentRefused
    from kiro_crew.connected_computers.flag import DISABLED_REASON, preview_on
    from kiro_crew.connected_computers.identity import IdentityError, local_computer_name
    from kiro_crew.connected_computers.vault_doc import VaultDocError, open_vault
    from kiro_crew.gateway_identity import gateway_id

    # The gateway also requires its developer flag at connect and start; this
    # process may not carry the gateway's environment, so it reads the config.
    if not preview_on():
        _print({"ok": False, "error": DISABLED_REASON})
        return 2
    body = _read_stdin()
    if body is None:
        _print({"ok": False, "error": "a JSON request is required on stdin"})
        return 2
    gateway = gateway_id(create=False)
    if not gateway:
        _print({"ok": False, "error": "this gateway has no id yet; start it once"})
        return 2
    renew = body.get("renew") is True
    fingerprint = str(body.get("fingerprint") or "")
    owner = getpass.getuser()
    try:
        registry = DeviceRegistry(open_vault())
        renew_cgen = None
        old = ""
        if renew:
            # A renewal presents the credential it replaces; one from before a
            # Disconnect, a Remove or a re-enrollment never renews, expired or not.
            old = str(body.get("credential") or "")
            renew_cgen = verify_for_renewal(
                old, gateway_id=gateway, registry=registry, fingerprint=fingerprint, owner=owner
            ).cgen
        record = registry.enroll(
            base=str(body.get("base") or ""),
            fingerprint=fingerprint,
            label=str(body.get("label") or ""),
            owner=owner,
            renew=renew,
            renew_cgen=renew_cgen,
        )
        credential, expires = mint(record, gateway)
        key = peer_key_from_secret(record, gateway)
    except CredentialRejected as exc:
        _print(
            {
                "ok": False,
                "error": f"this computer's access cannot be renewed ({exc.code})",
                "code": exc.code,
            }
        )
        return 2
    except EnrollmentRefused as exc:
        _print({"ok": False, "error": str(exc), "code": exc.code})
        return 2
    except (IdentityError, VaultDocError) as exc:
        _print({"ok": False, "error": str(exc)})
        return 2
    _reply_on_channel(
        {
            "ok": True,
            "device_id": record.device_id,
            "gateway_id": gateway,
            "gateway_label": local_computer_name(),
            "credential": credential,
            "expires": expires,
            "peer_key": key,
        }
    )
    return 0


def _remote_forget() -> int:
    from kiro_crew.connected_computers.devices import DeviceRegistry
    from kiro_crew.connected_computers.vault_doc import VaultDocError, open_vault

    body = _read_stdin() or {}
    registry = DeviceRegistry(open_vault())
    try:
        record = registry.get(str(body.get("device_id") or ""))
        if record is None or record.fingerprint != body.get("fingerprint"):
            _print({"ok": False, "error": "no such computer"})
            return 2
        registry.revoke(record.device_id)
    except VaultDocError as exc:
        _print({"ok": False, "error": str(exc)})
        return 2
    _print({"ok": True})
    return 0


def _remote_devices(as_json: bool) -> int:
    from kiro_crew.connected_computers.devices import DeviceRegistry
    from kiro_crew.connected_computers.vault_doc import open_vault

    rows = [r.public() for r in DeviceRegistry(open_vault()).list()]
    if as_json:
        _print({"devices": rows})
    else:
        for row in rows:
            print(f"{row['device_id']}\t{row['status']}\t{row['label']}")
    return 0


def _remote_end(device_id: str, *, remove: bool) -> int:
    from kiro_crew.connected_computers.devices import DeviceRegistry
    from kiro_crew.connected_computers.vault_doc import open_vault

    registry = DeviceRegistry(open_vault())
    record = (registry.remove if remove else registry.revoke)(device_id)
    if record is None:
        print(f"no computer {device_id!r} is enrolled here", file=sys.stderr)
        return 1
    print(f"{record.device_id}: {record.status}; an open connection ends within 15 seconds")
    return 0


def _store() -> Any:
    from kiro_crew.connected_computers.local_store import LocalGrantStore
    from kiro_crew.connected_computers.vault_doc import open_vault

    return LocalGrantStore(open_vault())


def _local_status(as_json: bool) -> int:
    rows = [g.public() for g in _store().list()]
    if as_json:
        _print({"grants": rows})
    else:
        for row in rows:
            print(f"{row['grant_id']}\t{row['status']}\t{row['device_id']}\t{row['root']}")
    return 0


def _instance_lookup() -> Any:
    from kiro_crew.instances.registry import InstancesRegistry

    registry = InstancesRegistry()
    return registry.get


def _wait_for_local_stop() -> None:
    """Give a running local gateway time to see the Off and stop its processes.

    It re-reads the grants every ``RECONCILE_SECS`` and stops each connector
    within ``STOP_BOUND_SECS``; a remote revoke is sent only after that.
    """
    import time

    from kiro_crew.connected_computers.controller import RECONCILE_SECS, STOP_BOUND_SECS

    time.sleep(RECONCILE_SECS + STOP_BOUND_SECS)


def _tell_remotes(store: Any) -> tuple[int, int]:
    """(confirmed, still pending) after one pass over the queued remote revokes."""
    from kiro_crew.connected_computers.controller import drain_pending_forgets

    confirmed = asyncio.run(drain_pending_forgets(store, _instance_lookup()))
    return confirmed, len(store.pending_forgets())


def _local_off() -> int:
    from kiro_crew.connected_computers.vault_doc import VaultDocError

    store = _store()
    try:
        # Persisted first, with the Off generation and the queued remote revokes:
        # it needs no network and no OS keyring.
        ended = store.revoke_all()
    except VaultDocError as exc:
        print(f"could not turn access off: {exc}", file=sys.stderr)
        return 2
    print(f"Turned off {len(ended)} grant(s) on this computer; they refuse all work now.")
    if not store.pending_forgets():
        return 0
    print("Waiting for the local gateway to stop their processes...")
    _wait_for_local_stop()
    confirmed, pending = _tell_remotes(store)
    if confirmed:
        print(f"Told {confirmed} remote gateway(s).")
    if pending:
        print(
            f"{pending} remote gateway(s) could not be reached; the local gateway "
            "tells them when it can. Access stays off here meanwhile."
        )
    return 0


def _local_reset_protection() -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "this needs an interactive terminal: run it yourself, not from a script",
            file=sys.stderr,
        )
        return 2
    print("This turns off every grant and forgets this computer's protected keys.")
    print("Each remote gateway will see this computer as new when you share it again.")
    if input("Type reset to continue, anything else to cancel: ").strip() != "reset":
        print("Cancelled; nothing changed.")
        return 1
    store = _store()
    ended = store.reset_protection()
    print(f"Turned off {len(ended)} grant(s) and reset protection. Nothing was deleted.")
    return 0


def run(args: argparse.Namespace) -> int:
    command = args.cc_command
    if command == "enroll":
        return _remote_enroll()
    if command == "forget":
        return _remote_forget()
    if command == "devices":
        return _remote_devices(args.json)
    if command in ("revoke", "remove"):
        return _remote_end(args.device_id, remove=command == "remove")
    if command == "status":
        return _local_status(args.json)
    if command == "off":
        return _local_off()
    if command == "reset-protection":
        return _local_reset_protection()
    if command == "desktop-host":
        from kiro_crew.connected_computers.desktop_host import run_desktop_host

        return run_desktop_host()
    raise SystemExit(2)
