"""The enterprise build's one sign-in: who is signed in, for every enterprise check.

Design: ``docs/request-for-change/rfc-crew-enterprise-edition.md``, D4. The login check
at start (:func:`kiro_crew.enterprise_boot.require_kiro_login`), the gate
(:class:`SignInLoginSource`), the rules fetch
(:mod:`kiro_crew.platform.enterprise_policy_fetch`) and the account lock (its profile
pin) all read :func:`select`, so they can never name different accounts.

Two sources, picked by presence and never mixed:

1. Crew's own vault (``kirocrew kas login``, the dashboard's Sign in). When the vault
   holds an entry, it is the sign-in and the only one. A lapsed entry (one that can no
   longer produce a token) or an unreadable vault is "not signed in", never a reason
   to read kiro-cli; the message says how to sign in again, and that removing Crew's
   sign-in hands over to kiro-cli's. A lapsed entry still names its account, so the
   account lock never reads it as a change of account.
2. kiro-cli's own sign-in (``kiro-cli login``), only when the vault holds no entry: the
   token row and the ``api.codewhisperer.profile`` row in kiro-cli's own store. Crew
   never renews kiro-cli's token, so an expired one is "not signed in". A store that
   exists but cannot be read or queried is "unusable" (fail closed), never "no
   sign-in": only a store with no token row is absent.

Both stored for different accounts is decided by the caller's :class:`Policy`: the
enterprise build refuses, naming both (D4); the ``oss`` build uses the vault and logs
one warning naming both.

Every refusal is a :class:`SignInError` carrying one of the ``CAUSE_*`` values, so the
boot error picks its fix from the real cause.

This module does not read the edition. Its callers decide which :class:`Policy`
applies: the enterprise wiring (``enterprise_boot``, the enterprise gate, the
enterprise policy fetch and doctor's enterprise row) uses :data:`ENTERPRISE`, the
default; the ``oss`` spawn owner (:func:`kiro_crew.auth.bridge.vault_holds_identity`)
uses :data:`OSS`.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import re
import shlex
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class Source(str, enum.Enum):
    """Where a sign-in came from."""

    VAULT = "vault"
    KIRO_CLI = "kiro_cli"


#: What to call each source in front of a person.
SOURCE_LABELS = {
    Source.VAULT: "Kiro Crew's own sign-in",
    Source.KIRO_CLI: "kiro-cli's own sign-in",
}

#: kiro-cli's sign-in command, as a person types it.
KIRO_CLI_SIGN_IN_COMMAND = "kiro-cli login"


@dataclass(frozen=True)
class Policy:
    """What one edition requires of the selection. The only edition-shaped input.

    ``refuse_mismatch``: a kiro-cli sign-in stored beside the vault's for another
    account is refused (enterprise, D4), instead of the vault being used with one
    warning (oss). ``require_profile``: a sign-in with no profile ARN is refused as
    not an organisation account (enterprise); oss accepts a Builder ID sign-in.
    """

    refuse_mismatch: bool
    require_profile: bool


ENTERPRISE = Policy(refuse_mismatch=True, require_profile=True)
OSS = Policy(refuse_mismatch=False, require_profile=False)

#: The sign-in verb, with no start URL. ``kas login --provider idc`` refuses to run
#: without one, so a screen that shows it must say where the start URL comes from.
_SIGN_IN_ARGV = ("kas", "login", "--provider", "idc", "--start-url")
#: The command with the bare launcher name. Only for a reader that must not learn a
#: host path (a non-owner's status); everything that prints a command to run uses
#: :func:`vault_sign_in_command`.
BARE_SIGN_IN_COMMAND = "kirocrew " + " ".join(_SIGN_IN_ARGV)
#: A PowerShell argument that needs no quotes.
_PS_BARE_RE = re.compile(r"^[A-Za-z0-9_\-./:=@]+$")


def _ps_quote(arg: str) -> str:
    """One PowerShell argument: bare when plain, else single-quoted with ``'`` doubled."""
    if arg and _PS_BARE_RE.fullmatch(arg):
        return arg
    return "'" + arg.replace("'", "''") + "'"


def render_command(argv: "list[str] | tuple[str, ...]", *, platform: str | None = None) -> str:
    """*argv* as one line a person pastes into this platform's shell. The one renderer.

    POSIX shells get :func:`shlex.join`. Windows gets the PowerShell form: the call
    operator ``&`` so a quoted launcher path runs, then each argument single-quoted
    when it needs it, with any ``'`` doubled.
    """
    if (platform or sys.platform) == "win32":
        return "& " + " ".join(_ps_quote(arg) for arg in argv)
    return shlex.join(argv)


def _launcher() -> str:
    """This install's own ``kirocrew``, or the bare name when none resolves.

    Not a bare ``kirocrew`` when a path is known: the desktop AppImage puts no
    ``kirocrew`` on PATH, so the bare name fails there with "command not found".
    """
    try:
        from kiro_crew.agent import _resolve_kirocrew_bin

        return _resolve_kirocrew_bin() or "kirocrew"
    except Exception:  # noqa: BLE001 - a remedy string never raises
        logger.debug("Enterprise sign-in command: launcher not resolved", exc_info=True)
        return "kirocrew"


def sign_in_argv(start_url: str = "", region: str = "") -> list[str]:
    """The sign-in command for THIS install as an argv list. The one builder."""
    argv = [_launcher(), *_SIGN_IN_ARGV]
    if start_url:
        argv.append(start_url)
        if region:
            argv += ["--region", region]
    return argv


def vault_sign_in_command() -> str:
    """The sign-in command for THIS install, with no start URL, ready to paste."""
    return render_command(sign_in_argv())


def sign_in_hint() -> str:
    """How to sign in, for a message that has found no usable sign-in."""
    return f"`{vault_sign_in_command()}` followed by your organisation's start URL"


_START_URL_RE = re.compile(r"^https://[^\s\"'`$\\<>|;&()]+$")
#: The region grammar ``auth.service.begin_device`` accepts (its ``_REGION_RE``).
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d{1,2}$")

# The real cause of a refused enterprise start. Read by enterprise_boot to pick the fix.
CAUSE_NO_SIGN_IN = "no_sign_in"
CAUSE_SIGN_IN_UNUSABLE = "sign_in_unusable"
CAUSE_UNREACHABLE = "unreachable"
CAUSE_REFUSED = "refused"
CAUSE_NOT_ELIGIBLE = "not_eligible"
CAUSE_ACCOUNT_CHANGED = "account_changed"
CAUSE_BAD_RULES = "bad_rules"
CAUSE_SIGN_INS_DISAGREE = "sign_ins_disagree"

TOKEN_TYPE_EXTERNAL_IDP = "EXTERNAL_IDP"

#: SEL audit label for the read of kiro-cli's own store.
KIRO_CLI_STORE_READ_ID = "enterprise_sign_in.kiro_cli_store"
#: A kiro-cli token this close to expiry counts as expired.
_EXPIRY_MARGIN_SECS = 60.0
_MAX_ARN_CHARS = 200

#: Seconds to wait for the vault read and token refresh.
_CREDENTIAL_TIMEOUT_SECS = 30.0
#: The oldest a gate check may be before the next one re-reads the store.
_MAX_CHECK_AGE_SECS = 60.0


class SignInError(RuntimeError):
    """No usable sign-in. ``str()`` is safe to print; ``cause`` is a ``CAUSE_*`` value."""

    def __init__(self, message: str, cause: str, source: Optional[Source] = None) -> None:
        super().__init__(message)
        self.cause = cause
        #: The source the refusal is about, when it is about one stored sign-in.
        self.source = source


@dataclass(frozen=True)
class Selection:
    """The one answer every check reads: which source, which account, and its token.

    ``source`` and ``profile_arn`` come from what is STORED (presence, never
    liveness). ``problem`` is set when the stored sign-in is lapsed (it cannot produce
    a token): the account is still known, but nothing may use it. ``token`` is empty
    unless the caller asked for one; it is never printed.
    """

    source: Source
    profile_arn: str
    problem: Optional[SignInError] = None
    token: str = ""
    token_type: str = ""

    def __repr__(self) -> str:
        return (
            f"Selection(source={self.source.value!r}, profile_arn={self.profile_arn!r}, "
            f"problem={self.problem!r}, token=<redacted>)"
        )

    @property
    def label(self) -> str:
        return SOURCE_LABELS[self.source]


def run_coroutine(coro: Any, *, timeout: float = _CREDENTIAL_TIMEOUT_SECS) -> Any:
    """Run *coro* to completion on its own loop in a worker thread.

    Callers are synchronous code that may itself be running on an event loop thread
    (boot) or on the policy refresher's thread, so running a loop here directly could
    find one already running.
    """
    box: Dict[str, Any] = {}

    def work() -> None:
        loop = asyncio.new_event_loop()
        try:
            box["value"] = loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller
            box["error"] = exc
        finally:
            loop.close()

    worker = threading.Thread(target=work, name="enterprise-sign-in-credential", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError("the Kiro sign-in did not answer in time")
    if "error" in box:
        raise box["error"]
    return box["value"]


# ── messages ────────────────────────────────────────────────────────────────


def _no_sign_in_message() -> str:
    return (
        f"no Kiro sign-in was found; sign in with `{KIRO_CLI_SIGN_IN_COMMAND}`, "
        f"or with {sign_in_hint()}"
    )


def _no_profile_message(source: Source = Source.VAULT) -> str:
    return (
        f"{SOURCE_LABELS[source]} has no profile, so it is not an organisation "
        f"account; sign in with an organisation account: {sign_in_hint()}"
    )


def _vault_logout_command() -> str:
    return render_command([_launcher(), "kas", "logout"])


def _unusable(detail: str) -> SignInError:
    return SignInError(
        f"{SOURCE_LABELS[Source.VAULT]} {detail}; sign in again with {sign_in_hint()}. "
        f"Removing it (`{_vault_logout_command()}`) makes Kiro Crew use kiro-cli's "
        "sign-in instead",
        CAUSE_SIGN_IN_UNUSABLE,
        Source.VAULT,
    )


def _kiro_cli_unusable(detail: str) -> SignInError:
    return SignInError(
        f"{SOURCE_LABELS[Source.KIRO_CLI]} {detail}; sign in again with "
        f"`{KIRO_CLI_SIGN_IN_COMMAND}`",
        CAUSE_SIGN_IN_UNUSABLE,
        Source.KIRO_CLI,
    )


def _disagree_message(vault_arn: str, cli_arn: str) -> str:
    return (
        f"{SOURCE_LABELS[Source.VAULT]} ({vault_arn}) and "
        f"{SOURCE_LABELS[Source.KIRO_CLI]} ({cli_arn}) are different accounts"
    )


# ── Crew's vault ────────────────────────────────────────────────────────────


def _vault_entry() -> Any:
    """The vault's stored identity, ``None`` when it holds none, or raise.

    Presence, not liveness: a lapsed entry is still returned. A vault that cannot be
    read, or holds an entry that will not decrypt, raises (fail closed), so it is never
    read as "no sign-in".
    """
    try:
        from kiro_crew.auth.bridge import default_token_store

        store = default_token_store()
        if store.holds_no_identity():
            return None
        token = store.resolve()
    except Exception:
        logger.debug("Kiro Crew sign-in vault could not be read", exc_info=True)
        raise _unusable("could not be read") from None
    if token is None:
        raise _unusable("could not be read")
    return token


def _vault_rejected(token: Any) -> bool:
    """Is *token* expired with a refresh token the issuer has already refused?

    ``is_usable`` cannot know that without a network call; the refresher records the
    refusal (``TokenStore.refresh_rejected``), and saving a new sign-in clears it.
    """
    if not token.is_expired():
        return False
    try:
        from kiro_crew.auth.bridge import default_token_store

        store = default_token_store()
        # This process's memory first: it holds a refusal whose marker write failed.
        if store.refused_in_process(token):
            return True
        # Strict read: a marker that exists but cannot be read raises, and fails closed.
        return store.refresh_rejected_strict(token.identity) is not None
    except Exception:
        logger.debug("Kiro Crew sign-in refusal marker could not be read", exc_info=True)
        return True


def _vault_token(expected_arn: str) -> Selection:
    """The vault's sign-in with a live access token, refreshed if due, or raise."""
    from kiro_crew.auth.bridge import default_token_store
    from kiro_crew.auth.provider import KasAuthProvider, NotAuthenticated

    provider = KasAuthProvider(default_token_store(), allow_env_api_key=False)
    try:
        token = run_coroutine(provider.current())
    except NotAuthenticated:
        raise SignInError(_no_sign_in_message(), CAUSE_NO_SIGN_IN) from None
    except Exception as exc:
        raise _unusable(f"could not be read or refreshed ({type(exc).__name__})") from None
    if not token.profile_arn:
        raise SignInError(_no_profile_message(), CAUSE_NOT_ELIGIBLE, Source.VAULT)
    if token.profile_arn != expected_arn:
        # The vault was rewritten between the selection and the refresh.
        raise _unusable("changed while it was being read")
    token_type = TOKEN_TYPE_EXTERNAL_IDP if token.auth_method == "external_idp" else ""
    return Selection(Source.VAULT, token.profile_arn, None, token.access_token, token_type)


# ── kiro-cli's own sign-in ──────────────────────────────────────────────────


@dataclass(frozen=True)
class _CliEntry:
    """One token row of kiro-cli's store. The token is never printed."""

    profile_arn: str
    token: str = field(repr=False)
    token_type: str
    expires_at: Optional[datetime]

    def live(self, now: datetime) -> bool:
        if not self.token or self.expires_at is None:
            return False
        return (self.expires_at - now).total_seconds() > _EXPIRY_MARGIN_SECS


def _kiro_cli_store() -> Path:
    """kiro-cli's own store path (fixed, fenced anchors; no environment variable).

    Every anchor is checked first and any error but "not there" raises: the selector's
    ``exists()`` reads an anchor it cannot reach as missing, and choosing another,
    absent anchor would then read as "no sign-in".
    """
    from kiro_crew import identity_stores

    home = Path.home()
    for candidate in identity_stores.kiro_cli_store_candidates(sys.platform, home):
        try:
            os.lstat(candidate)
        except FileNotFoundError:
            continue
    return identity_stores.selected_store(sys.platform, home)


def _expiry(raw: object) -> Optional[datetime]:
    """A token row's expiry, through the one shared parser; a missing one is ``None``."""
    from kiro_crew import identity_stores

    if not isinstance(raw, str) or not raw:
        return None
    return identity_stores.parse_token_expiry(raw.strip())


def _kiro_cli_rows(con: Any) -> list[_CliEntry]:
    """Every token row in the open store. Raises on any query or decoding failure.

    A token row that is not there is "nothing stored". A store without its
    ``auth_kv`` table, or anything that is there and cannot be read, raises, so the
    caller reports the store unusable, never absent.
    """
    from kiro_crew import identity_stores

    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    arn = ""
    if "state" in tables:
        row = con.execute(
            "SELECT value FROM state WHERE key=?", (identity_stores.AUTH_PROFILE_STATE_KEY,)
        ).fetchone()
        if row:
            data = json.loads(row[0])
            found = data.get("arn") if isinstance(data, dict) else None
            if isinstance(found, str) and found.startswith("arn:aws:codewhisperer:"):
                arn = found[:_MAX_ARN_CHARS]
    entries: list[_CliEntry] = []
    if "auth_kv" not in tables:
        # kiro-cli's store always carries this table; a file without it (an empty
        # or truncated file reads as a database with no tables) is damaged.
        raise ValueError("kiro-cli's store has no auth_kv table")
    for key in identity_stores.AUTH_TOKEN_KEYS:
        row = con.execute("SELECT value FROM auth_kv WHERE key=?", (key,)).fetchone()
        if not row:
            continue
        blob = json.loads(row[0])
        if not isinstance(blob, dict):
            raise ValueError("a token row is not an object")
        token = blob.get("access_token") or blob.get("accessToken") or ""
        entries.append(
            _CliEntry(
                profile_arn=arn,
                token=token if isinstance(token, str) else "",
                token_type=(
                    TOKEN_TYPE_EXTERNAL_IDP
                    if key == identity_stores.AUTH_EXTERNAL_IDP_TOKEN_KEY
                    else ""
                ),
                expires_at=_expiry(blob.get("expires_at") or blob.get("expiresAt")),
            )
        )
    return entries


def _kiro_cli_entry() -> Optional[_CliEntry]:
    """kiro-cli's stored sign-in, ``None`` when it stores none, or raise (fail closed).

    ``None`` only for a store that is not there or holds no token row. A store that
    exists but cannot be opened, queried or decoded raises ``sign_in_unusable`` --
    never ``no_sign_in``, which the account lock reads as a sign-out. Audited on every
    outcome; an entry is returned only once its ``success`` audit is recorded.
    """
    from kiro_crew import hooks
    from kiro_crew.kiro_prerequisite import _open_identity_db_readonly

    try:
        path = _kiro_cli_store()
        # Not ``os.path.lexists``: it answers False for a permission error too, and
        # only a store that is truly not there may read as "no sign-in".
        os.lstat(path)
    except FileNotFoundError:
        return None
    except Exception:
        logger.debug("kiro-cli's sign-in store could not be located", exc_info=True)
        raise _kiro_cli_unusable("could not be read") from None
    con = _open_identity_db_readonly(path)
    if con is None:
        hooks.emit_internal_read_audit(KIRO_CLI_STORE_READ_ID, "unreadable")
        raise _kiro_cli_unusable("could not be read")
    try:
        entries = _kiro_cli_rows(con)
    except Exception as exc:
        # SQLite opens a damaged file read-only without complaint; the damage shows
        # at the first query, which is why the queries are where this fails closed.
        logger.debug("kiro-cli's sign-in store could not be read (%s)", type(exc).__name__)
        hooks.emit_internal_read_audit(KIRO_CLI_STORE_READ_ID, "unreadable")
        raise _kiro_cli_unusable("could not be read") from None
    finally:
        con.close()
    if not entries:
        hooks.emit_internal_read_audit(KIRO_CLI_STORE_READ_ID, "no_token")
        return None
    now = datetime.now(timezone.utc)
    chosen = next((entry for entry in entries if entry.live(now)), entries[0])
    if not hooks.emit_internal_read_audit(KIRO_CLI_STORE_READ_ID, "success"):
        logger.debug("kiro-cli sign-in read denied: audit unavailable")
        raise _kiro_cli_unusable("could not be read")
    return chosen


#: Account pairs already warned about in this process (oss mismatch: one warning).
_warned_mismatches: set = set()


def _mismatch(vault_arn: str, policy: Policy) -> None:
    """Refuse or warn when kiro-cli stores another account beside the vault's.

    Only a kiro-cli sign-in that names a profile can disagree. kiro-cli is not the
    sign-in here, so its store failing to read is logged and decides nothing.
    """
    if not vault_arn:
        return
    try:
        cli = _kiro_cli_entry()
    except SignInError:
        logger.debug("kiro-cli store unreadable beside the vault sign-in", exc_info=True)
        return
    if cli is None or not cli.profile_arn or cli.profile_arn == vault_arn:
        return
    message = _disagree_message(vault_arn, cli.profile_arn)
    if policy.refuse_mismatch:
        raise SignInError(
            f"{message}; sign in to one account, or remove one of them "
            f"(`{_vault_logout_command()}`)",
            CAUSE_SIGN_INS_DISAGREE,
        )
    pair = (vault_arn, cli.profile_arn)
    if pair not in _warned_mismatches:
        _warned_mismatches.add(pair)
        logger.warning("%s; Kiro Crew uses its own sign-in", message)


# ── the resolver ────────────────────────────────────────────────────────────


def _select(policy: Policy) -> Tuple[Selection, Optional[_CliEntry]]:
    vault = _vault_entry()
    if vault is not None:
        vault_arn = vault.profile_arn or ""
        if not vault_arn and policy.require_profile:
            raise SignInError(_no_profile_message(), CAUSE_NOT_ELIGIBLE, Source.VAULT)
        _mismatch(vault_arn, policy)
        problem = None
        if not vault.is_usable() or _vault_rejected(vault):
            problem = _unusable("has expired and cannot be renewed")
        return Selection(Source.VAULT, vault_arn, problem), None
    cli = _kiro_cli_entry()
    if cli is None:
        raise SignInError(_no_sign_in_message(), CAUSE_NO_SIGN_IN)
    if not cli.profile_arn and policy.require_profile:
        raise SignInError(_no_profile_message(Source.KIRO_CLI), CAUSE_NOT_ELIGIBLE, Source.KIRO_CLI)
    problem = None
    if not cli.live(datetime.now(timezone.utc)):
        problem = _kiro_cli_unusable("has expired")
    return Selection(Source.KIRO_CLI, cli.profile_arn, problem), cli


def select(*, policy: Policy = ENTERPRISE) -> Selection:
    """The one sign-in, by presence, or raise :class:`SignInError`.

    A vault entry is the sign-in; with none, kiro-cli's. Raises for no sign-in, a store
    that cannot be read, a sign-in with no profile (when *policy* requires one), and
    two accounts (when *policy* refuses that). A lapsed sign-in is returned with
    ``problem`` set. A plain store read: no refresh, no network, no spawn.
    """
    return _select(policy)[0]


def resolve(*, with_token: bool = False, policy: Policy = ENTERPRISE) -> Selection:
    """The selected sign-in (:func:`select`) when it is usable, or raise.

    *with_token* also returns a live access token, for the rules fetch: the vault's
    refreshed if due, or kiro-cli's as stored (Crew never renews it), from the same
    entry the selection named.
    """
    chosen, cli = _select(policy)
    if chosen.problem is not None:
        raise chosen.problem
    if not with_token:
        return chosen
    if chosen.source is Source.VAULT:
        return _vault_token(chosen.profile_arn)
    if cli is None:  # a kiro-cli selection always carries its entry
        raise _kiro_cli_unusable("could not be read")
    return replace(chosen, token=cli.token, token_type=cli.token_type)


def describe() -> str:
    """One line for ``kirocrew doctor``: which sign-in an enterprise build uses."""
    try:
        sign_in = resolve()
    except SignInError as exc:
        return f"❌ {exc}"
    except Exception as exc:  # doctor must not crash on a broken store
        return f"⚠  could not check ({type(exc).__name__})"
    return f"✅ {sign_in.label} ({sign_in.profile_arn})"


def sign_in_command() -> tuple[str, bool]:
    """The command that signs the vault in again, and whether it is complete.

    A lapsed vault entry still records the start URL it was signed in with, so the
    command names it and runs as copied. With no recorded URL the command ends at
    ``--start-url`` and the second value is False: the caller says where to find it.
    Never raises; a store that cannot be read gives the bare command.
    """
    try:
        from kiro_crew.auth.bridge import default_token_store
        from kiro_crew.auth.whoami import recorded_start_url

        token = default_token_store().resolve()
        start_url = recorded_start_url(token) if token is not None else ""
        region = (token.region or "") if token is not None else ""
    except Exception:  # noqa: BLE001 - a status read never raises
        logger.debug("Enterprise sign-in command: vault unreadable", exc_info=True)
        start_url = region = ""
    if not (start_url and _START_URL_RE.match(start_url)):
        return vault_sign_in_command(), False
    # Without the stored region, `kas login` starts the sign-in in the Builder ID
    # region, the wrong one for an Identity Center outside us-east-1.
    if not (isinstance(region, str) and _REGION_RE.fullmatch(region)):
        region = ""
    return render_command(sign_in_argv(start_url, region)), True


class SignInLoginSource:
    """The enterprise gate's login source (``enterprise_gate.LoginSource``), from :func:`resolve`.

    ``authenticated`` is the latched verdict of the last check. ``login_verified``
    re-checks on every call while signed out, and otherwise when the last check is
    older than *max_age_secs* (at most a minute). Each check is a plain
    store read, run off the event loop.
    """

    def __init__(self) -> None:
        self._ok = self._check()
        self._at = time.monotonic()

    @staticmethod
    def _check() -> bool:
        """Signed in, and (once the account lock has pinned) as the pinned account."""
        try:
            found = resolve()
            from kiro_crew.platform import enterprise_policy_fetch as epf

            pin = epf.pinned_scope()
            if pin and epf.profile_scope(found.profile_arn) != pin:
                logger.warning("Enterprise sign-in check: %s", epf.ACCOUNT_CHANGED)
                return False
        except SignInError as exc:
            logger.warning("Enterprise sign-in check: %s", exc)
            return False
        except Exception:
            logger.warning("Enterprise sign-in check raised", exc_info=True)
            return False
        return True

    @property
    def authenticated(self) -> bool:
        return self._ok

    async def login_verified(self, *, max_age_secs: float) -> bool:
        # A plain store read is cheap. While signed out every check reads again, so
        # a fresh sign-in reopens the gate on the watch's next check. While signed
        # in a sign-out is seen within a minute even when the watch's budget is longer.
        if not self._ok or time.monotonic() - self._at >= min(max_age_secs, _MAX_CHECK_AGE_SECS):
            self._ok = await asyncio.to_thread(self._check)
            self._at = time.monotonic()
        return self._ok
