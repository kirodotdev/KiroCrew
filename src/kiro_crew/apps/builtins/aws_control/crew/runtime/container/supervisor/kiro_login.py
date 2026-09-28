"""Getting kiro-cli past its own login check when the crew vault owns the identity.

``kiro-cli acp`` validates its own credential store BEFORE it offers an ACP
handshake. On a store it has never signed into it exits ``rc=1`` with "You are not
logged in, please log in with kiro-cli login", so the ``_kiro/auth/getAccessToken``
request the vault answers is never reached: the task serves ``/health`` 200 and
answers every dashboard turn with ``503 kiro_prerequisite_required``. The vault seed
is correct and lands; the refusal sits in front of it.

So the supervisor writes ONE row into that store, and the row is a NON-SECRET
SENTINEL rather than a copy of any credential. It carries a labelled placeholder
access token, no refresh token, and a fixed far-future expiry. Nothing in it comes
from the vault, so nothing in it is worth reading.

THAT IS SOUND BECAUSE THE ROW IS A GATE PASS, NOT A CREDENTIAL. Crew is the auth
owner: ``acp/kas_transport.build_kas_argv`` omits ``--auth-method cli``, the engine
raises its credential request on the wire, and
``acp/kas_host_auth.answer_get_access_token`` answers it from the vault inside the
backend process. Measured on the binary this image pins: with this exact row in the
store, ``kiro-cli acp --agent-engine v3`` starts and logs
``Auth: --auth=acp-callback (host-mediated refresh via _kiro/auth/getAccessToken)``.
The engine asks the host for the token it uses; the row only answers the question
"has this store been signed into".

A SENTINEL IS THE SMALLEST HONEST ROW, and writing the real identity here would be
strictly worse. This store is pinned OUT of the sandbox masking tiers on purpose, so
a raw ``open()`` from a spawned shell reads it, and the model worker auto-approves
every tool it calls on untrusted prompt content. The container withholds the
credential from that worker's environment for exactly this reason
(``backend.build_backend_env``); putting it in a world-visible file would reopen the
route by another door. The sentinel's expiry is also a fixed far-future constant rather
than the vault's, so a long-lived task's later spawns pass the same check as its first,
and an aged delivery the vault can still renew is not turned into a startup refusal.

This module runs in the container supervisor and nowhere else, which is what keeps it
internal-only: a desktop host signs kiro-cli in itself.

WHAT THE STORE IS. Measured against the binary this image pins
(``Dockerfile`` ``KIRO_VERSION``, whose tarball is sha256-checked there), not from
memory and not from a newer local install:

- ``$XDG_DATA_HOME/kiro-cli/data.sqlite3``, falling back to
  ``$HOME/.local/share/kiro-cli/data.sqlite3``. Confirmed by running the binary under
  each environment and finding the file. ``KIRO_HOME`` does NOT move it.
- Table ``auth_kv (key TEXT PRIMARY KEY, value TEXT)``, written with
  ``INSERT OR REPLACE``. Both statements are in the binary verbatim.
- Values are PLAIN JSON. No keyring, no encryption, nothing opaque.
- The Builder ID row's key is spelled ``odic`` rather than ``oidc``. That is the
  binary's spelling, and copying it is not a typo here.

WHAT THE ROW MUST CONTAIN. The seven ``BuilderIdToken`` field names are in the
binary, and each was confirmed by acceptance -- a row was written and the binary was
asked whether the store then reads as signed in:

- Every KEY must be present. A two-key row reads as not signed in. ``region`` alone
  carries a default.
- The optional values may be JSON ``null``: ``refresh_token``, ``region``,
  ``start_url`` and ``scopes`` are all null here and the store reads as signed in.
- ``oauth_flow`` takes no null. Both spellings the binary accepts pass its check;
  the one it writes for its own SSO-OIDC rows is used.
- The expiry must be outside the binary's own refresh margin, which measures between
  60 and 200 seconds. An expired row is rejected: the binary tries to renew it and
  this store carries no device registration to renew it with.

THE DATABASE IS CREATED BY THE BINARY, never by this module. ``auth_kv`` comes from a
migration whose ``CREATE TABLE`` carries no ``IF NOT EXISTS``, so a table this module
made first would break the binary's own migration on the next open. When the file is
absent the binary is run once (:data:`LOGIN_CHECK_ARGV`) purely so it lays down its
schema, and its exit status is ignored there -- "not logged in" is the expected answer
at that point and is not a failure.

AND THE BINARY IS ASKED WHETHER IT ACCEPTS THE ROW, which is the one check that makes
the shape above a verified claim rather than a remembered measurement. Reading the row
back out of sqlite proves only that sqlite kept the bytes this module just wrote; it
cannot fail. So after the write the binary's own login check runs again and a non-zero
status is a startup refusal. What that buys is a ``KIRO_VERSION`` bump whose store
contract moved: without the probe the seed reports success while the relay refuses
every turn, which is the healthy-looking task answering 503 that this module exists to
remove, arrived at a second way. With it, that drift is loud and names the version.

Every failure here is a startup refusal, for the same reason.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
from collections.abc import Mapping
from pathlib import Path

from .. import common

log = logging.getLogger("container.supervisor.kiro_login")

#: Where the store lives, relative to the data root the binary resolves.
STORE_DIR_NAME: str = "kiro-cli"
STORE_FILE_NAME: str = "data.sqlite3"
STORE_HOME_PARTS: tuple[str, ...] = (".local", "share")

#: Env names the binary reads to resolve that root, in its own order.
ENV_XDG_DATA_HOME: str = "XDG_DATA_HOME"
ENV_HOME: str = "HOME"

#: The table and the exact statements the binary uses, so this row is
#: indistinguishable from one it wrote itself.
AUTH_TABLE: str = "auth_kv"
UPSERT_SQL: str = f"INSERT OR REPLACE INTO {AUTH_TABLE} (key, value) VALUES (?1, ?2)"
SELECT_SQL: str = f"SELECT value FROM {AUTH_TABLE} WHERE key = ?1"
#: Emptied before the row is written, for two reasons that want the same statement. A
#: persistent volume can carry a row an EARLIER task wrote, and this task must not be
#: signed in as whoever that was; and with nothing else in the table, the binary's own
#: verdict afterwards is about THIS row rather than about whatever else was lying
#: there. Only kiro-cli's auth rows live in this table, and inside this container
#: nothing else ever signs it in.
CLEAR_SQL: str = f"DELETE FROM {AUTH_TABLE}"

#: The binary's own key for its SSO-OIDC row, spelled as it spells it.
SENTINEL_ROW_KEY: str = "kirocli:odic:token"

#: The placeholder that stands where a credential would be. It is labelled so that
#: anyone who finds it in the store, or in a log, can tell at a glance that it is not
#: a token and that the host answers for the real one.
SENTINEL_ACCESS_TOKEN: str = "kiro-crew-host-owns-auth-not-a-credential"

#: A fixed far-future expiry, in the wire format the binary's own rows carry:
#: RFC3339, UTC, Z-suffixed, with a fractional part. Fixed rather than derived from a
#: clock or from the vault, because nothing about this row should go stale: the
#: binary re-reads the store on every spawn, and an expiry that lapses mid-task would
#: put the refusal back for every spawn after it.
SENTINEL_EXPIRES_AT: str = "2099-01-01T00:00:00.000000Z"

#: ``oauth_flow`` takes no null, and this is the value the binary writes for its own
#: SSO-OIDC rows. Inert here: it is compared against a stored device registration
#: during the binary's own refresh, and this row carries no refresh token and no
#: registration -- the vault owns renewal.
SENTINEL_OAUTH_FLOW: str = "DeviceCode"

#: The binary's OWN login check, in the smallest command that runs it. Invoked twice
#: for two different questions: once before the write, when the store file is absent,
#: purely so the binary lays down its own schema (status ignored, "not logged in" is
#: the expected answer there); and once after the write, where a zero status is
#: REQUIRED -- that is the whole verification, see :func:`_require_accepted`.
LOGIN_CHECK_ARGV: tuple[str, ...] = ("whoami",)

#: How long either call may take before it is treated as a failure. It is a local read
#: on a store with no refreshable token in it; the bound exists so a hung binary cannot
#: hold container startup open.
LOGIN_CHECK_TIMEOUT_SECS: int = 60


def store_path(env: Mapping[str, str] | None = None) -> Path:
    """The store file the binary would open under ``env``.

    Resolved the way the binary resolves it rather than from a path this image
    happens to set: hard-coding either name would write to a path the binary stops
    reading the moment a base image or a task definition sets the other.
    """
    source = os.environ if env is None else env
    root = (source.get(ENV_XDG_DATA_HOME) or "").strip()
    if root:
        return Path(root) / STORE_DIR_NAME / STORE_FILE_NAME
    home = (source.get(ENV_HOME) or "").strip()
    if not home:
        raise common.ConfigError(
            f"cannot locate kiro-cli's login store: neither {ENV_XDG_DATA_HOME} nor "
            f"{ENV_HOME} is set in this process. kiro-cli resolves its own store from "
            "those two and refuses to start a turn when that store holds no identity, "
            "so a guessed path would look seeded and still fail every turn."
        )
    return Path(home).joinpath(*STORE_HOME_PARTS) / STORE_DIR_NAME / STORE_FILE_NAME


def sentinel_row() -> tuple[str, str]:
    """``(key, json)`` for the non-secret row that satisfies the binary's check.

    A constant, taking no argument and reading nothing: the row asserts that this
    store has been signed into, which is a local fact about who owns auth, and it
    carries no identity, no account and no credential to get wrong.
    """
    document = {
        "access_token": SENTINEL_ACCESS_TOKEN,
        "expires_at": SENTINEL_EXPIRES_AT,
        "refresh_token": None,
        "region": None,
        "start_url": None,
        "oauth_flow": SENTINEL_OAUTH_FLOW,
        "scopes": None,
    }
    return SENTINEL_ROW_KEY, json.dumps(document)


def _resolve_binary() -> str | None:
    """The absolute path to kiro-cli, or None.

    Resolved through ``kiro_crew.kiro_cli.resolve_kiro_cli``, which is the same
    resolution ``acp/client`` performs before it spawns the relay: if this cannot find
    the binary, nothing in the task can, and an absolute path is what keeps ``exec``
    from re-resolving a bare argv0 off the inherited PATH.

    Imported inside the function, NOT at module scope, for the reason spelled out on
    ``backend.seed_model_identity``: this tree is also imported standalone as a
    top-level ``container`` package with no ``kiro_crew`` importable, and
    ``supervisor/__init__.py`` re-exports from here.
    """
    from kiro_crew.kiro_cli import resolve_kiro_cli

    return resolve_kiro_cli()


def _run_login_check(binary: str, *, run, env: Mapping[str, str] | None):
    """Run the binary's own login check. Returns whatever ``run`` returns."""
    argv = [binary, *LOGIN_CHECK_ARGV]
    try:
        return run(
            argv,
            check=False,
            capture_output=True,
            timeout=LOGIN_CHECK_TIMEOUT_SECS,
            env=dict(os.environ if env is None else env),
        )
    except (OSError, subprocess.SubprocessError) as err:
        raise common.ConfigError(
            f"could not run kiro-cli's own login check ({argv!r}): "
            f"{type(err).__name__}: {err}. Refusing to start -- that check is what the "
            "relay runs before every ACP handshake, so a task that cannot run it here "
            "would fail every turn."
        ) from err


def _prepare_store(
    path: Path, binary: str, *, run=subprocess.run, env: Mapping[str, str] | None = None
) -> None:
    """Make sure the store file exists, by asking the BINARY to create it."""
    if path.exists():
        return
    # Exit status IGNORED here: on an empty store the expected answer is "not logged
    # in" with a non-zero status, and that is the state this call exists to create a
    # schema for, not a failure. The status is required LATER, by _require_accepted,
    # once there is a row for the check to accept.
    _run_login_check(binary, run=run, env=env)
    if not path.exists():
        raise common.ConfigError(
            f"kiro-cli did not create its login store at {path} when run as "
            f"{[binary, *LOGIN_CHECK_ARGV]!r}. The location is resolved from "
            f"{ENV_XDG_DATA_HOME}/{ENV_HOME} the way kiro-cli resolves it, so a file "
            "somewhere else means this image's kiro-cli reads a path this seed does not "
            "write. Refusing to start."
        )


def _require_accepted(
    binary: str, path: Path, *, run=subprocess.run, env: Mapping[str, str] | None = None
) -> None:
    """Refuse unless the BINARY now reports the store as signed in.

    This is the check that makes the row's shape a verified claim rather than a
    remembered measurement. Reading the row back out of sqlite proves only that sqlite
    kept the bytes this module just wrote -- it cannot fail. Whether the key spelling,
    the field set and the expiry contract are the ones THIS image's kiro-cli accepts is
    a different question, and the binary is right here to answer it.

    The verdict is about THIS row because the table was emptied first
    (:data:`CLEAR_SQL`): with another acceptable row still in it, a pass would say only
    that something in the store satisfies the check.

    Which matters most at a ``KIRO_VERSION`` bump. A store contract that moves would
    otherwise leave the seed reporting success while the relay refuses every turn --
    the same healthy-looking task answering 503 that this module exists to remove, with
    nothing anywhere saying why. With this probe that drift is a loud startup refusal
    naming the version.
    """
    completed = _run_login_check(binary, run=run, env=env)
    code = getattr(completed, "returncode", None)
    if code != 0:
        raise common.ConfigError(
            f"kiro-cli does not accept its login store at {path} after the row was "
            f"written (its own {LOGIN_CHECK_ARGV[-1]!r} check exited {code!r}). The row "
            "shape is measured against the kiro-cli this image pins, so the likely cause "
            "is that the pinned version moved and its store contract moved with it: the "
            "key spelling, the required field set, or the expiry rule. Refusing to start "
            "-- the relay runs this same check before every handshake, so every turn "
            "would fail with nothing saying why."
        )


def seed_kiro_cli_login(
    *,
    env: Mapping[str, str] | None = None,
    run=subprocess.run,
) -> Path:
    """Write the sentinel row into kiro-cli's own store. Returns the store path.

    Takes no settings and reads no vault. Whether the task HAS an identity is
    ``backend.require_model_identity``'s question, answered from the vault
    immediately before this runs; this function's only question is whether kiro-cli
    will start, and the answer to that is the same row whatever the vault holds.
    """
    key, value = sentinel_row()
    path = store_path(env)
    binary = _resolve_binary()
    if binary is None:
        raise common.ConfigError(
            "kiro-cli could not be resolved, so its login store cannot be prepared and "
            "the relay every turn runs through cannot start. The image installs it, and "
            "this is the same resolution the relay's own spawn performs, so a run that "
            "reaches here without it is misbuilt."
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        raise common.ConfigError(
            f"could not create the directory for kiro-cli's login store "
            f"({path.parent}): {err}. Refusing to start."
        ) from err
    _prepare_store(path, binary, run=run, env=env)
    try:
        with sqlite3.connect(path) as conn:
            conn.execute(CLEAR_SQL)
            conn.execute(UPSERT_SQL, (key, value))
            conn.commit()
            stored = conn.execute(SELECT_SQL, (key,)).fetchone()
    except sqlite3.Error as err:
        raise common.ConfigError(
            f"could not write kiro-cli's login store at {path} "
            f"({type(err).__name__}: {err}). The table is {AUTH_TABLE!r}, created by "
            "kiro-cli's own migration; an error naming it means this image's kiro-cli "
            "keeps its credential somewhere else than the binary that was measured. "
            "Refusing to start rather than letting every turn fail its pre-handshake "
            "check."
        ) from err
    # Read back before claiming anything: a write that landed is not a row the reader
    # accepts, and the whole value of this seed is that the reader accepts it.
    if stored is None or stored[0] != value:
        raise common.ConfigError(
            f"kiro-cli's login store did not keep the row written for key {key!r} at "
            f"{path}. Refusing to start."
        )
    # Then ask the BINARY whether it accepts the store, which is the only check that
    # can tell a row it will serve from a row sqlite merely kept.
    _require_accepted(binary, path, run=run, env=env)
    log.info("kiro-cli accepts its login store at %s (row %s)", path, key)
    return path
