"""``kirocrew secrets authorize``: the owner binds a Custom secret to one exact https origin.

``_handle_secrets_authorize`` runs host-side under the owner: under a cross-process
lock it reads the policy (inside ``secret-request-policy/``, a directory every agent
sandbox hides whole), verifies the existing owner signature, applies the change,
re-signs the authorizations map with a key derived from the dashboard token-signing
secret, and publishes the policy atomically inside that same hidden directory.
An agent shell cannot reproduce this -- the token-signing secret is unreadable inside
the sandbox -- which is what lets the host loader tell an owner-authored policy from an
agent-planted one.

An owner of the mirror facade ``kiro_crew.cli_commands``: callers and tests reach every
name here as ``kiro_crew.cli_commands.<name>``, and a patch through the facade reaches
this module. This module never imports the facade.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import stat
import sys
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.sel import sel

# The policy, provenance, data-home and atomic-write helpers are imported inside the
# functions that use them: an owner of the ``kiro_crew.cli_commands`` mirror binds no
# module-level name the facade does not already bind, or the facade would serve it.


def _spawn_precreates_policy_directory() -> bool:
    """Whether agent spawns on this host go through the Linux namespace backend.

    The same answer the spawn path reaches: the operator's ``agent.sandbox`` tier,
    clamped by any governed floor, handed to ``detect_backend``. Anything that
    cannot be answered reads as ``True``, which keeps the refuse-unless-precreated
    behaviour rather than creating a directory a running namespace may not mask.
    """
    try:
        from kiro_crew.sandbox_tiers import configured_sandbox_mode, effective_sandbox_mode
        from kiro_crew.sandbox_wrap import detect_backend

        mode = effective_sandbox_mode(configured_sandbox_mode())
        return detect_backend(config_mode=mode) == "namespace"
    except Exception:  # noqa: BLE001 -- fail closed to the precreate-only path
        return True


def _handle_secrets_authorize(args: argparse.Namespace) -> None:
    """Owner authorizes (or removes) a Custom secret's mediated egress origin and
    re-signs the policy so trusted host code can confirm owner provenance.

    Runs host-side under the owner: it reads/writes the policy directly (inside
    the directory the agent sandbox hides) and signs the authorizations map
    with the gateway-owned member key. An agent shell cannot reproduce this — the
    signing key is unreadable inside the sandbox — which is exactly what lets the
    loader distinguish an owner-authored policy from an agent-planted one.
    """
    # ``origin`` is an optional positional so ``--remove`` can de-authorize by
    # name alone (the remove path never reads it). When ADDING, the origin
    # is mandatory — enforce it here rather than at the parser, which cannot
    # express "required unless --remove".
    if not args.remove and not args.origin:
        print(
            "error: the 'origin' argument is required when authorizing a secret "
            "(omit it only with --remove).",
            file=sys.stderr,
        )
        sys.exit(2)
    from kiro_crew.config import loader as _loader
    from kiro_crew.secrets_mediation.policy import POLICY_LOCK_FILENAME, policy_path
    from kiro_crew.secrets_mediation.provenance import REQUEST_POLICY_DIRNAME

    cfg_dir = _loader.config_dir()
    path = policy_path(cfg_dir)
    directory = cfg_dir / REQUEST_POLICY_DIRNAME
    # Only the Linux namespace backend hides the directory with a bind mount, which
    # needs the name to exist BEFORE the spawn: there it is precreated, masked, before
    # every agent spawn and never created here, so the CLI is never the one to create
    # a directory a sandbox started before the mask existed would then see unmasked.
    # A Seatbelt deny is a path rule that holds for a name that does not exist yet,
    # and with the sandbox off nothing masks it, so on those hosts nothing would ever
    # precreate it and authorize could never run: create it here, owner-only.
    # ``mkdir`` does not follow a link at the final name; the check below decides.
    if not _spawn_precreates_policy_directory():
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            print(f"error: cannot create {directory}: {exc}", file=sys.stderr)
            sys.exit(1)
    if platform_compat.is_link_or_junction(directory) or not directory.is_dir():
        print(
            f"error: {directory} is missing or is not a real directory; start the "
            "gateway and open one chat (that creates the hidden directory before any "
            "agent runs), then re-run.",
            file=sys.stderr,
        )
        sys.exit(1)
    # Serialize the ENTIRE read-verify-modify-sign-write across processes: two
    # concurrent `authorize` runs would otherwise each read the same policy,
    # update their own copy, and have the last atomic write silently drop the
    # other's entry. A lockfile beside the policy (owner-only) is the mutex.
    lock_path = directory / POLICY_LOCK_FILENAME
    # Open the lock NO-FOLLOW as an alias-free regular file. If a sandboxed agent
    # swapped the lock path for a symlink or a fresh inode between two concurrent
    # authorizations, the two runs could lock DIFFERENT inodes and the last write
    # would silently drop an authorization. O_NOFOLLOW refuses a link (POSIX-only,
    # a no-op flag on Windows), so a path-level lstat first refuses a symlink on
    # every platform; the regular-file + single-link fstat then rejects a swapped
    # alias by inode.
    try:
        try:
            if stat.S_ISLNK(os.lstat(lock_path).st_mode):
                raise RuntimeError(
                    "the secrets authorize lock path is a symlink; refusing to proceed "
                    "so a swapped lock inode cannot drop an authorization."
                )
        except FileNotFoundError:
            pass  # not yet created — os.open below creates a fresh regular file
        lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(lock_path, lock_flags, 0o600)
    except (RuntimeError, OSError) as exc:
        # A planted symlink/hardlink/special file or an O_NOFOLLOW link race at
        # the owner-only lock path fails safe (nothing is read or written yet),
        # but must surface as a concise CLI error, not an uncaught traceback.
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        st = os.fstat(lock_fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise RuntimeError(
                "the secrets authorize lock file is not an alias-free regular file; "
                "refusing to proceed so a swapped lock inode cannot drop an authorization."
            )
        with platform_compat.file_lock(lock_fd, exclusive=True, required=True):
            _authorize_txn(args, path)
    except (RuntimeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        os.close(lock_fd)


def _publish_secret_request_policy(path: Path, content: str) -> None:
    """Publish the policy atomically, inside the wholly hidden policy directory.

    The sandbox masks the DIRECTORY, so the staging temp ``atomic_write`` creates
    beside the policy is hidden like the policy itself, and the rename lands on a
    name inside the masked directory -- never on the dentry a mask is mounted on.
    (A rename over a masked FILE name makes Linux lazily detach that mount in every
    namespace already running, which would expose the real policy to the agent and
    let it replay a signed copy after the owner revokes an origin.)
    """
    from kiro_crew.atomic_write import atomic_write, fsync_dir

    atomic_write(path, content, fsync=True, restrict_to_owner=True)
    fsync_dir(path.parent)


def _authorize_txn(args: "argparse.Namespace", path) -> None:
    """The locked read-verify-modify-sign-write body of ``secrets authorize``."""
    from kiro_crew.secrets_mediation.policy import (
        _MAX_POLICY_BYTES,
        PolicyError,
        _parse_placement,
        normalize_origin,
    )
    from kiro_crew.secrets_mediation.provenance import (
        SIGNATURE_FIELD,
        _secret_policy_key,
        sign_secret_authorizations,
        verify_secret_authorizations,
    )

    try:
        if path.exists():
            # Bounded read: cap the bytes taken from disk BEFORE the JSON decode so
            # a planted oversized (or sparse) pre-upgrade policy file cannot
            # exhaust the CLI process's memory. The agent can author this file on a
            # host where the sandbox mask does not apply, so the read must be
            # bounded here exactly as the host endpoint bounds its own read. Read
            # cap+1 and refuse on overflow — same ceiling, no drift.
            with open(path, "rb") as fh:
                raw_bytes = fh.read(_MAX_POLICY_BYTES + 1)
            if len(raw_bytes) > _MAX_POLICY_BYTES:
                raise SystemExit(
                    f"error: {path} exceeds the {_MAX_POLICY_BYTES}-byte policy size "
                    "limit; refusing to read it. Fix or remove the file and re-run."
                )
            raw_existing = json.loads(raw_bytes.decode("utf-8"))
        else:
            raw_existing = {}
    except (OSError, ValueError) as exc:
        # A present-but-unreadable/unparseable policy is NOT silently replaced:
        # overwriting it could drop owner authorizations we simply failed to read.
        # Abort without writing unless the owner explicitly resets (the recovery
        # for a document a crash mid-write left torn).
        if not getattr(args, "reset", False):
            raise SystemExit(
                f"error: {path} could not be read as JSON ({exc}); "
                "refusing to overwrite it. Fix or remove the file, or re-run with "
                "--reset to discard it."
            )
        raw_existing = {}
    if not isinstance(raw_existing, dict):
        # Valid JSON but not an object (e.g. a list or scalar). Calling .get() on
        # it would raise AttributeError; overwriting it could drop owner data.
        # Abort without writing rather than crash or clobber.
        raise SystemExit(
            f"error: {path} is not a JSON object; refusing to overwrite it. "
            "Fix or remove the file and re-run."
        )
    existing = raw_existing
    existing_auth = existing.get("authorizations")
    # When the signing secret cannot be READ, verification is impossible — we cannot
    # tell a legitimately owner-signed policy from a tampered one. Discarding here
    # would silently ERASE existing owner authorizations just because the secret is
    # unreadable. Refuse to overwrite a nonempty policy in that case. (An unreadable
    # secret is distinct from a signature that does not verify — the latter is an
    # agent-plant, a tamper or a rotated token key, handled below.)
    if isinstance(existing_auth, dict) and existing_auth and _secret_policy_key() is None:
        raise SystemExit(
            "error: the dashboard token-signing secret (token_signing.key) cannot be read, "
            f"so {path} cannot be verified; refusing to overwrite its existing "
            "authorizations. Fix the data home's permissions and re-run."
        )
    # Carry forward existing authorizations ONLY if the file is already validly
    # owner-signed. An unsigned/tampered map (what an agent could plant before the
    # owner ever ran this command, or before the sandbox mask applied on upgrade)
    # must NOT be preserved-and-re-signed — that would launder the agent's
    # destination into an owner signature. Start from an empty map in that case.
    if (
        isinstance(existing_auth, dict)
        and existing_auth
        and verify_secret_authorizations(existing_auth, existing.get(SIGNATURE_FIELD))
    ):
        authorizations = existing_auth
    elif isinstance(existing_auth, dict) and existing_auth:
        # A nonempty policy whose signature does NOT verify (tampered, agent-planted,
        # or signed under a different key). Do NOT silently discard it — that would
        # erase whatever it holds, including a legitimate policy we merely can't
        # confirm. Refuse unless the owner EXPLICITLY resets, which is the audited
        # way to say "I know this file is not mine, throw it away and start over".
        if not getattr(args, "reset", False):
            raise SystemExit(
                f"error: {path} exists but is not validly owner-signed (tampered, or "
                "signed before token_signing.key was rotated); refusing to overwrite it. "
                "Re-run with --reset to intentionally discard it and start a fresh signed "
                "policy."
            )
        print(
            "warning: --reset given; discarding the existing unverifiable "
            "mediated-secret policy and starting a fresh owner-signed policy.",
            file=sys.stderr,
        )
        authorizations = {}
    else:
        authorizations = {}

    name = args.name
    if args.remove:
        authorizations.pop(name, None)
    else:
        try:
            origin = normalize_origin(args.origin)
            placement = (
                {"type": "header", "header": args.header} if args.header else {"type": "bearer"}
            )
            # Validate the placement the same way the loader will, up front.
            _parse_placement(placement, name)
        except PolicyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        authorizations[name] = {"origin": origin, "placement": placement}

    doc = {"version": 1, "authorizations": authorizations, SIGNATURE_FIELD: ""}
    try:
        doc[SIGNATURE_FIELD] = sign_secret_authorizations(authorizations)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Audit the authorization change BEFORE publishing the policy: granting or
    # removing a credential egress is a security-relevant control edit, so it must
    # never land unaudited. Emit the secret-free SEL event first (the NAME, the
    # action, and on grant the authorized ORIGIN only; never a secret value) and
    # ABORT the write if the PRE-write audit cannot be recorded — a durable
    # egress-permission change with no audit trail is exactly what this record
    # exists to prevent. The pre-write event is recorded as ``attempted``, not
    # ``ok``: the write below can still fail (full disk, EIO, read-only fs), and
    # recording success before publication would leave SEL permanently asserting
    # a change that never landed. Success is recorded only AFTER atomic_write
    # returns.
    if args.remove:
        _resources = f"secret={name} action=remove"
    else:
        _resources = f"secret={name} action=authorize origin={authorizations[name]['origin']}"
    try:
        sel().log_api_access(
            caller="owner",
            operation="mediated_secret_authorize",
            outcome="attempted",
            source="cli",
            resources=_resources,
            critical=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"error: could not record the required audit event for this "
            f"authorization change ({type(exc).__name__}); refusing to change a "
            f"credential-egress permission without an audit trail.",
            file=sys.stderr,
        )
        sys.exit(1)

    serialized = json.dumps(doc, indent=2) + "\n"
    # Refuse BEFORE publishing if the serialized policy would exceed the SAME
    # byte bound the reader enforces: a near-limit policy plus a new authorization
    # can push the file past _MAX_POLICY_BYTES, where the reader (which caps its
    # read at that bound) rejects it — every authorization becomes unreadable, a
    # self-inflicted data loss. The reader bounds the raw BYTES (it reads the file
    # in binary), so measure the UTF-8 encoded length here with the exact constant
    # the reader uses, no drift. The existing policy on disk stays intact and
    # readable because the write never happens.
    if len(serialized.encode("utf-8")) > _MAX_POLICY_BYTES:
        print(
            f"error: this authorization would grow the policy past the "
            f"{_MAX_POLICY_BYTES}-byte size limit the reader enforces; refusing to "
            f"publish a policy that the reader would reject (the existing policy is "
            f"left unchanged). Remove an unused authorization and re-run.",
            file=sys.stderr,
        )
        sys.exit(1)

    _publish_secret_request_policy(path, serialized)
    # Publication succeeded: record the terminal ``ok`` outcome. A best-effort
    # log here (the change is already durable, so a missing success row must not
    # undo it); the ``attempted`` row above is the guaranteed trail.
    try:
        sel().log_api_access(
            caller="owner",
            operation="mediated_secret_authorize",
            outcome="ok",
            source="cli",
            resources=_resources,
            critical=True,
        )
    except Exception:  # noqa: BLE001 — the change is durable; a missing ok row is not fatal
        logging.getLogger("kiro_crew.cli_commands").warning(
            "mediated_secret_authorize succeeded but its success audit could not be recorded"
        )
    if args.remove:
        print(f"Removed mediated-request authorization for {name!r}.")
    else:
        print(f"Authorized {name!r} for {authorizations[name]['origin']} (policy signed).")
