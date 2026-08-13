"""Inbound-webhook support: token store, run history ring, context freshness.

Three concerns live here, all of them shared between the ``POST
/api/hooks/agent`` execution path and the dashboard's ``/api/webhooks``
management endpoints:

* :class:`WebhookTokenStore` — multiple named bearer tokens persisted as
  sha256 hashes only. The raw secret is returned exactly once, by
  :meth:`WebhookTokenStore.create`, and is unrecoverable afterwards.
* :func:`verify_signature` / :func:`sign_payload` — HMAC-SHA256 request
  signing over ``f"{timestamp}.{raw_body}"``, with a ±:data:`SIGNATURE_WINDOW_SECONDS`
  replay window and a bounded seen-signature set.
* :class:`WebhookRunStore` — a bounded ring of the last
  :data:`MAX_RUNS` webhook runs (including rejections and auth failures)
  so the UI can show real history instead of guessing.
* :func:`context_freshness` / :func:`resolve_context` — the single
  implementation of the three-horizon context decay. The dashboard badge
  and the actual injection both call it, so the badge can never claim
  "fresh" for context the agent would not receive.

Both stores use the same on-disk discipline as ``register_hook``:
read-modify-write under an advisory ``flock`` on a sidecar ``.lock`` file,
then an atomic ``os.replace`` of a 0600 temp file.

SECRET AT REST — ``webhook_tokens.json`` is no longer hash-only. Bearer tokens
are still stored as sha256 digests and cannot be recovered from the file, but
each entry's ``signing_secret`` is a LIVE secret held in plaintext: an HMAC is
symmetric, so the verifier has to be able to recompute it. That is an accepted
tradeoff of offering request signing at all, not an oversight. Consequences:
the file stays mode 0600, ``public_entries()`` strips ``signing_secret`` exactly
as it strips ``token_hash``, and anything that serialises a stored entry onto a
response must go through ``public_entries()`` or strip the key itself.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.validation import sanitize_string

logger = logging.getLogger(__name__)

# ── Token model ──

TOKEN_PREFIX = "kc_whk_"
TOKEN_ENTROPY_CHARS = 32
MAX_TOKENS = 20
LABEL_MAX_LEN = 64

LEGACY_TOKEN_ID = "legacy"
LEGACY_TOKEN_LABEL = "Legacy token (config)"

TOKENS_FILENAME = "tokens.json"
# The credential store lives in its OWN directory, not beside the other crew
# files, because the sensitive-path gate classifies whole directories and the
# store's temp files have to be covered as well as the store itself.
#
# Gating only the final filename was not enough: ``write_json_atomic`` publishes
# through ``mkstemp`` + ``os.replace``, so a same-UID agent with file-write tools
# could write its own bearer hash into the not-yet-renamed ``*.tmp`` inode (the
# 0600 mode does not stop the same user) and the rename would then publish it as
# the live store — minting a credential for ``/api/hooks/agent``, which is on the
# dashboard-auth bypass list. Naming the DIRECTORY in the gate covers the store,
# its lock file and every temp file in one rule, which is the same treatment the
# other trust roots (``profiles``, ``run``) already get.
#
# Deliberately NOT migrated from the pre-move location: adopting a file from an
# ungated path would re-open exactly the vector this closes (an agent could plant
# a chosen hash there and have the gateway promote it). The surface is new in this
# change, so there is no released data to carry over; anyone who ran it from the
# branch regenerates their credentials.
SECRETS_DIRNAME = "webhooks"
RUNS_FILENAME = "webhook_runs.json"

# ── Request signing (HMAC-SHA256) ──
#
# The bearer token proves WHO is calling; the signature proves the body arrived
# unmodified and is not a replay. They are independent gates and both are
# enforced when a token has ``require_signature``.

SIGNING_SECRET_PREFIX = "kc_whs_"
#: ``secrets.token_urlsafe(32)`` is exactly this many url-safe chars.
SIGNING_SECRET_ENTROPY_CHARS = 43

TIMESTAMP_HEADER = "X-KiroCrew-Timestamp"
SIGNATURE_HEADER = "X-KiroCrew-Signature"
SIGNATURE_SCHEME = "sha256="

#: Accepted clock skew either side of ``now`` for the signed timestamp.
SIGNATURE_WINDOW_SECONDS = 300
#: Cap on the DIGITS accepted in the timestamp header. Python ints are arbitrary
#: precision, so without this a 309-digit value parses and then raises
#: OverflowError when compared against a float ``now`` — a 500 on an otherwise
#: authenticated request. A unix second needs 10 digits.
TIMESTAMP_MAX_DIGITS = 20
#: Cap on the replay seen-set. Entries also age out after the window, so this
#: only binds under a flood of distinct valid signatures.
SIGNATURE_SEEN_MAX = 4096

# One distinct error string per failure cause: the caller has to be able to tell
# "you forgot the headers" from "your clock is off" from "your body was
# rewritten by a proxy" without server log access.
SIG_ERR_MISSING = "signature required"
SIG_ERR_TIMESTAMP = "invalid signature timestamp"
SIG_ERR_WINDOW = "signature timestamp outside window"
SIG_ERR_MALFORMED = "malformed signature header"
SIG_ERR_MISMATCH = "signature mismatch"
SIG_ERR_REPLAY = "signature already used"
SIG_ERR_NO_SECRET = "signing secret unavailable"
#: The seen-set is full of signatures that are all still inside their window, so
#: replay protection cannot be guaranteed for one more. Distinct from REPLAY:
#: this call was not a replay, it was refused to avoid forgetting one.
SIG_ERR_REPLAY_CAPACITY = "replay protection saturated, retry shortly"

# ── Run history ──

MAX_RUNS = 50

OUTCOME_COMPLETED = "completed"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_ERROR = "error"
OUTCOME_REJECTED_CAPACITY = "rejected_capacity"
OUTCOME_UNAUTHORIZED = "unauthorized"
OUTCOME_DISABLED = "disabled"

VALID_OUTCOMES = frozenset(
    {
        OUTCOME_COMPLETED,
        OUTCOME_TIMEOUT,
        OUTCOME_ERROR,
        OUTCOME_REJECTED_CAPACITY,
        OUTCOME_UNAUTHORIZED,
        OUTCOME_DISABLED,
    }
)

# ── Context freshness ──

FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_EXPIRED = "expired"

#: Horizon 1 upper bound — context newer than this is injected verbatim.
FRESH_MAX_SECONDS = 3600.0
#: Horizon 2 upper bound — context newer than this is injected with a banner.
STALE_MAX_SECONDS = 24 * 3600.0

#: Transport cap for ``context_summary`` on the read endpoint.
CONTEXT_SUMMARY_TRANSPORT_MAX = 2000


class WebhookError(Exception):
    """Raised for caller-fixable webhook store errors (cap reached, bad label)."""


class WebhookStoreUnreadable(WebhookError):
    """Raised when a store file exists but cannot be read or parsed.

    A subclass of :class:`WebhookError` so the dashboard handlers' existing
    error mapping reports it rather than surfacing an unhandled 500.
    """


# ── Shared on-disk primitives ──


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock for read-modify-write on *path*.

    Public because ``hooks.json`` (owned by the ``register_hook`` MCP tool)
    is mutated from the dashboard handlers too and must use the same lock
    discipline.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / (path.name + ".lock")
    with open(lock_path, "w") as lock_fd:
        with platform_compat.flock_exclusive(lock_fd.fileno()):
            yield


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write *payload* as JSON to *path* atomically, owner-only.

    Call inside :func:`locked` so concurrent writers cannot lose updates.

    ``restrict_to_owner=True`` is the load-bearing argument here and is NOT a
    synonym for ``mode=0o600``: this store holds signing secrets, and on
    Windows a bare 0600 is a no-op, so the helper applies the owner-only DACL
    to the temp file BEFORE ANY PAYLOAD BYTE IS WRITTEN. Locking down after
    the write left a window where the secrets sat in a file carrying only the
    parent directory's inherited ACL. That ordering used to be hand-rolled
    here; it is now :func:`atomic_write`'s documented contract, which also
    brings the Windows ``os.replace`` sharing-violation retry this copy lacked.

    Content is ``bytes`` rather than ``str`` on purpose: ``indent=2`` embeds
    newlines, and text mode would translate them to CRLF on Windows.
    """
    atomic_write(
        path,
        json.dumps(payload, indent=2).encode("utf-8"),
        fsync=True,
        restrict_to_owner=True,
    )


def _read_json(path: Path, default: Any) -> Any:
    """Read JSON from *path*; return *default* only when the file is absent.

    A file that exists but cannot be read or parsed is NOT treated as empty.
    Returning the default there meant the next write serialised that default
    over the top: a single corrupt byte, a truncated tail, or a transient EACCES
    silently destroyed every issued token, its signing secret, and the run
    history — and, because the kill switch shares the token file, silently
    re-enabled webhooks. Failing closed keeps the bytes on disk for an operator
    to inspect or restore, at the cost of erroring the surface until they do.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as exc:
        logger.warning("webhook store unreadable (%s): %s", path, exc)
        raise WebhookStoreUnreadable(
            f"webhook store at {path.name} is unreadable; "
            "refusing to overwrite it"
        ) from exc


def sanitize_label(label: str) -> str:
    """Normalize a user-supplied token label; raise on empty/oversize."""
    cleaned = sanitize_string(str(label or "")).strip()
    if not cleaned:
        raise WebhookError("label is required")
    if len(cleaned) > LABEL_MAX_LEN:
        raise WebhookError(f"label exceeds {LABEL_MAX_LEN} chars")
    return cleaned


def hash_token(raw: str) -> str:
    """Return the sha256 hex digest persisted for *raw*."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── Token store ──


class WebhookTokenStore:
    """Multi-token store for inbound webhook bearer tokens (hashes only)."""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self._base = Path(base_dir) if base_dir is not None else config_dir()

    @property
    def path(self) -> Path:
        return self._base / SECRETS_DIRNAME / TOKENS_FILENAME

    # -- reads --

    def _load(self) -> list[dict[str, Any]]:
        """Stored token entries, or refuse if the shape is not the one we wrote.

        ``_read_json`` already fails closed on bytes it cannot parse. This is the
        same hazard one level up: JSON that parses fine but whose container is
        not a list, or whose rows are not credential rows, used to be filtered
        away silently — and every caller that mutates the store (``mint``,
        ``revoke``, ``stamp_used``) writes the loaded list straight back, so the
        filtered-out rows were deleted on the next write. That destroys issued
        credentials and their signing secrets, and because the kill switch lives
        in this same file it could also drop the disabled state. Refusing keeps
        the bytes on disk for an operator to inspect, at the cost of erroring the
        surface until they do.
        """
        raw = _read_json(self.path, {})
        if isinstance(raw, dict) and "tokens" not in raw:
            # No store yet, or a store that only carries the kill switch. Absent
            # is not malformed: there is nothing to lose by treating it as empty.
            return []
        entries = raw.get("tokens") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            raise WebhookStoreUnreadable(
                f"webhook store at {self.path.name} holds a "
                f"{type(entries).__name__} where a list of tokens belongs; "
                "refusing to overwrite it"
            )
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("token_hash"):
                raise WebhookStoreUnreadable(
                    f"webhook store at {self.path.name} holds a token row "
                    "without a usable hash; refusing to overwrite it"
                )
        return list(entries)

    def list_entries(self) -> list[dict[str, Any]]:
        """All stored entries, oldest first. Includes ``token_hash``."""
        return self._load()

    # -- kill switch --
    #
    # Separate from "are there any tokens": revoking every token is destructive
    # and not reversible, so an operator needs a way to shut the endpoint off
    # (incident response, a leaked token, a noisy integration) while keeping the
    # tokens and their history intact. Defaults to ON when the key is absent so
    # upgrading an install that already had ``hooks.webhook_token`` configured
    # does not silently break it — the endpoint is still closed unless a token
    # exists, so default-on does not widen the surface on a fresh install.

    def is_switch_on(self) -> bool:
        raw = _read_json(self.path, {})
        if not isinstance(raw, dict):
            return True
        return raw.get("enabled", True) is not False

    def set_switch(self, enabled: bool) -> bool:
        """Persist the kill switch. Returns the stored value."""
        with locked(self.path):
            raw = _read_json(self.path, {})
            if isinstance(raw, dict):
                data: dict[str, Any] = raw
            elif isinstance(raw, list):
                # A bare list IS a valid store: ``_load`` reads the top-level
                # list as the token rows. Coercing it to ``{}`` here would write
                # back an empty token list and destroy every credential and
                # signing secret on disk — the exact loss WebhookStoreUnreadable
                # exists to prevent. Lift it into its canonical shape instead.
                data = {"tokens": raw}
            else:
                data = {}
            data["enabled"] = bool(enabled)
            data.setdefault("tokens", [])
            write_json_atomic(self.path, data)
        return bool(enabled)

    def public_entries(self, legacy_token: str = "") -> list[dict[str, Any]]:
        """Entries safe to hand a dashboard client (no hash, no raw secret).

        A non-empty *legacy_token* is surfaced as a synthetic ``legacy``
        entry so setups still using ``hooks.webhook_token`` are visible.

        This is an allow-list, not a deny-list: the dict is rebuilt field by
        field, so ``token_hash`` and ``signing_secret`` cannot leak onto the
        read endpoint by someone later adding a field to the stored shape.
        """
        out: list[dict[str, Any]] = []
        if legacy_token:
            # Only the last 4 chars are surfaced: unlike minted tokens the
            # legacy value has no fixed public prefix, so echoing its head
            # would leak secret material to the dashboard.
            out.append(
                {
                    "id": LEGACY_TOKEN_ID,
                    "label": LEGACY_TOKEN_LABEL,
                    "display_prefix": "hooks.webhook_token",
                    "last4": legacy_token[-4:],
                    "created_at": 0.0,
                    "last_used_at": None,
                    "legacy": True,
                    # The config scalar has no signing secret to verify against,
                    # so it stays bearer-only rather than breaking on upgrade.
                    "require_signature": False,
                }
            )
        for entry in self._load():
            out.append(
                {
                    "id": entry.get("id", ""),
                    "label": entry.get("label", ""),
                    "display_prefix": entry.get("display_prefix", ""),
                    "last4": entry.get("last4", ""),
                    "created_at": float(entry.get("created_at") or 0.0),
                    "last_used_at": entry.get("last_used_at"),
                    "legacy": False,
                    "require_signature": bool(entry.get("require_signature")),
                }
            )
        return out

    def entry_for(self, token_id: str) -> dict[str, Any] | None:
        """Return the RAW stored entry for *token_id*, including its secret.

        Verifier-only accessor — the result carries ``signing_secret`` and
        ``token_hash``. Never hand it to a response; use
        :meth:`public_entries` for anything client-facing.
        """
        if not token_id:
            return None
        for entry in self._load():
            if str(entry.get("id", "")) == token_id:
                return entry
        return None

    def count(self) -> int:
        return len(self._load())

    # -- writes --

    def _write_tokens(self, entries: list[dict[str, Any]]) -> None:
        """Persist *entries* while preserving every other top-level key.

        The store file also carries the ``enabled`` kill switch, so writing
        ``{"tokens": [...]}`` wholesale would silently flip webhooks back on the
        next time a token was minted, revoked, or stamped. Callers must already
        hold the file lock.
        """
        raw = _read_json(self.path, {})
        data: dict[str, Any] = {k: v for k, v in raw.items() if k != "tokens"} \
            if isinstance(raw, dict) else {}
        data["tokens"] = entries
        write_json_atomic(self.path, data)

    def create(
        self, label: str, require_signature: bool = True
    ) -> tuple[str, str, dict[str, Any]]:
        """Mint a token. Returns ``(raw_secret, signing_secret, public_entry)``.

        The raw bearer secret is never persisted and cannot be recovered later.
        The signing secret IS persisted (an HMAC verifier needs it) but is
        returned here so the create response can reveal it once; every read path
        strips it. ``signing_secret`` is ``""`` when *require_signature* is
        false — a caller that cannot compute an HMAC gets a bearer-only token.
        """
        clean_label = sanitize_label(label)
        raw = TOKEN_PREFIX + secrets.token_urlsafe(32)[:TOKEN_ENTROPY_CHARS]
        signing_secret = (
            SIGNING_SECRET_PREFIX + secrets.token_urlsafe(32)[:SIGNING_SECRET_ENTROPY_CHARS]
            if require_signature
            else ""
        )
        entry = {
            "id": "wht_" + secrets.token_hex(3),
            "label": clean_label,
            "token_hash": hash_token(raw),
            "display_prefix": raw[: len(TOKEN_PREFIX) + 4],
            "last4": raw[-4:],
            "created_at": time.time(),
            "last_used_at": None,
            "require_signature": bool(require_signature),
            "signing_secret": signing_secret,
        }
        path = self.path
        with locked(path):
            entries = self._load()
            if len(entries) >= MAX_TOKENS:
                raise WebhookError(f"token limit reached ({MAX_TOKENS})")
            existing_ids = {e.get("id") for e in entries}
            while entry["id"] in existing_ids:
                entry["id"] = "wht_" + secrets.token_hex(3)
            entries.append(entry)
            self._write_tokens(entries)
        public = {
            k: v for k, v in entry.items() if k not in ("token_hash", "signing_secret")
        }
        public["legacy"] = False
        return raw, signing_secret, public

    def delete(self, token_id: str) -> bool:
        """Remove the entry with *token_id*. False when unknown."""
        path = self.path
        with locked(path):
            entries = self._load()
            kept = [e for e in entries if e.get("id") != token_id]
            if len(kept) == len(entries):
                return False
            self._write_tokens(kept)
        return True

    def stamp_used(self, token_id: str, when: float | None = None) -> None:
        """Record that *token_id* just authenticated a request."""
        path = self.path
        with locked(path):
            entries = self._load()
            touched = False
            for entry in entries:
                if entry.get("id") == token_id:
                    entry["last_used_at"] = when if when is not None else time.time()
                    touched = True
            if touched:
                self._write_tokens(entries)

    # -- verification --

    def verify(
        self,
        candidate: str,
        legacy_token: str = "",
        *,
        stamp_used: bool = True,
    ) -> str | None:
        """Return the id of the token matching *candidate*, else ``None``.

        Stored tokens are compared by sha256 digest with
        :func:`hmac.compare_digest`; the legacy config scalar is compared
        against the raw value it was configured with. A stored match normally
        stamps ``last_used_at`` before returning. Authentication flows that
        have a second factor (the webhook HMAC) pass ``stamp_used=False`` and
        stamp explicitly only after every factor has succeeded.
        """
        if not candidate:
            return None
        digest = hash_token(candidate)
        for entry in self._load():
            if hmac.compare_digest(str(entry.get("token_hash", "")), digest):
                token_id = str(entry.get("id", ""))
                if stamp_used:
                    try:
                        self.stamp_used(token_id)
                    except OSError:
                        logger.warning(
                            "webhook token last_used_at stamp failed", exc_info=True
                        )
                return token_id
        if legacy_token and hmac.compare_digest(candidate, legacy_token):
            return LEGACY_TOKEN_ID
        return None


# ── Request signing / verification ──
#
# Signed string is exactly ``f"{timestamp}.{raw_body}"`` over the bytes as
# received. Re-serialising a parsed body can never reproduce the caller's bytes
# (key order, separators, unicode escaping all differ), so the handler must read
# the raw body first and parse from it.
#
# The seen-signature set is deliberately in-memory and per-process: it closes
# the replay hole inside the ±window for the process that accepted the request,
# which is where a captured-request replay lands. Persisting it would put a disk
# write on every accepted call for no additional guarantee — the timestamp
# window is what bounds replay in absolute terms.

# signature hex -> time first accepted
_seen_signatures: dict[str, float] = {}
# Verification runs in asyncio.to_thread workers (the endpoint must not block the
# loop), so the eviction / membership-test / insertion below is a check-then-act
# across several threads. Without one lock over all three, two identical signed
# requests in flight at the same moment can both pass the membership test before
# either inserts, and both would start an agent turn — which is exactly the
# replay the set exists to prevent.
_seen_signatures_lock = threading.Lock()


def _expected_digest(secret: str, timestamp: str, body: bytes) -> str:
    """Hex HMAC-SHA256 of ``f"{timestamp}."`` + *body*, keyed with *secret*."""
    signed = timestamp.encode("utf-8") + b"." + body
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def sign_payload(secret: str, timestamp: int | str, body: bytes) -> str:
    """Return the ``sha256=<hex>`` header value for *body* at *timestamp*.

    Used by the dashboard's own test-request probe and by callers writing
    integration code against this contract.
    """
    return SIGNATURE_SCHEME + _expected_digest(secret, str(timestamp), body)


def _remember_signature(signature: str, sent_at: float, now: float) -> bool | None:
    """Record *signature* as accepted.

    ``True`` recorded, ``False`` already seen (a replay), ``None`` the set is
    saturated with still-valid entries so nothing can be recorded safely.

    Entries are keyed on the SIGNED timestamp, not on when they were inserted,
    because that is what the window check compares against. ``verify_signature``
    accepts while ``abs(now - sent_at) <= SIGNATURE_WINDOW_SECONDS``, and the
    window is symmetric, so a future-dated timestamp stays acceptable for up to
    twice the window in wall-clock terms. Evicting on insertion age dropped such
    an entry while its own timestamp was still valid, which re-admitted the exact
    replay this set exists to refuse.
    """
    with _seen_signatures_lock:
        for key, stamped_at in list(_seen_signatures.items()):
            if now - stamped_at > SIGNATURE_WINDOW_SECONDS:
                _seen_signatures.pop(key, None)
        if signature in _seen_signatures:
            return False
        if len(_seen_signatures) >= SIGNATURE_SEEN_MAX:
            # Every entry is still inside its window, so there is nothing safe to
            # drop: evicting the oldest would forget a signature that the window
            # check still accepts, and the captured request behind it would be
            # admitted a second time. Refuse instead — replay protection is the
            # guarantee, and shedding load is the lesser failure. Reaching this
            # needs SIGNATURE_SEEN_MAX distinct signed calls inside one window.
            return None
        _seen_signatures[signature] = sent_at
        return True


def verify_signature(
    *,
    secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    now: float | None = None,
) -> str | None:
    """Verify an inbound request signature. ``None`` means accepted.

    Any other return value is one of the ``SIG_ERR_*`` strings naming the
    specific cause, suitable for both the 401 response body and the recorded
    run detail. Checks run in the documented order — headers present, timestamp
    parses and is inside the window, digest matches, signature not replayed —
    so the caller is told the first thing that is actually wrong.

    A signature is only added to the replay set once it has passed every other
    check, so a bad-digest flood cannot evict live entries.
    """
    stamp = time.time() if now is None else now
    if not secret:
        return SIG_ERR_NO_SECRET
    if not timestamp or not signature:
        return SIG_ERR_MISSING
    # The digest is recomputed over the timestamp EXACTLY as sent (modulo
    # surrounding whitespace), not over a re-rendered int, so a caller whose
    # integer rendering differs from Python's still verifies.
    sent_raw = str(timestamp).strip()
    # Bound the digit count BEFORE parsing. Python ints are arbitrary precision,
    # so a 309-digit timestamp parses happily and then raises OverflowError ("int
    # too large to convert to float") on the comparison against `stamp`, turning
    # an authenticated caller's bad header into a 500. A unix second needs 10
    # digits; the cap leaves generous room for a sign and far-future values while
    # keeping the value inside float range.
    if len(sent_raw.lstrip("+-")) > TIMESTAMP_MAX_DIGITS:
        return SIG_ERR_TIMESTAMP
    try:
        sent_at = int(sent_raw)
    except (TypeError, ValueError):
        return SIG_ERR_TIMESTAMP
    if abs(stamp - sent_at) > SIGNATURE_WINDOW_SECONDS:
        return SIG_ERR_WINDOW
    candidate = str(signature).strip()
    if not candidate.startswith(SIGNATURE_SCHEME):
        return SIG_ERR_MALFORMED
    provided = candidate[len(SIGNATURE_SCHEME):].lower()
    if not provided or any(c not in "0123456789abcdef" for c in provided):
        return SIG_ERR_MALFORMED
    expected = _expected_digest(secret, sent_raw, body)
    if not hmac.compare_digest(provided, expected):
        return SIG_ERR_MISMATCH
    remembered = _remember_signature(provided, float(sent_at), stamp)
    if remembered is None:
        return SIG_ERR_REPLAY_CAPACITY
    if not remembered:
        return SIG_ERR_REPLAY
    return None


def _reset_signature_replay() -> None:
    """Test hook — the seen-signature set is process-global module state."""
    _seen_signatures.clear()


# ── Run history ring ──


class WebhookRunStore:
    """Bounded ring of the last :data:`MAX_RUNS` inbound-webhook runs."""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self._base = Path(base_dir) if base_dir is not None else config_dir()

    @property
    def path(self) -> Path:
        return self._base / RUNS_FILENAME

    def _load(self) -> list[dict[str, Any]]:
        """Rows as stored, filtered only for structural validity.

        Deliberately does NOT drop unknown outcomes. ``record`` rewrites whatever
        this returns, so filtering here made a write delete rows it merely failed
        to recognise — a row written by a build with more outcomes than this one
        (a downgrade, or a rolling deploy) was destroyed by the next recorded run.
        The client-facing filter belongs on the read that feeds the client, which
        is :meth:`list_runs`.
        """
        raw = _read_json(self.path, {})
        runs = raw.get("runs") if isinstance(raw, dict) else raw
        if not isinstance(runs, list):
            return []
        return [r for r in runs if isinstance(r, dict)]

    def list_runs(self) -> list[dict[str, Any]]:
        """Recorded runs, newest first, excluding outcomes this build cannot render.

        The dashboard indexes its label/badge table by outcome, so an unknown
        value arrives as a missing entry and takes the whole page down with its
        error boundary. Skipping the row costs one line of history; letting it
        through costs the page. This is a display filter only — the row stays on
        disk, and ``record`` preserves it.
        """
        return [r for r in self._load() if r.get("outcome") in VALID_OUTCOMES]

    def record(
        self,
        *,
        outcome: str,
        hook_id: str | None = None,
        session_key: str | None = None,
        name: str = "",
        started_at: float | None = None,
        duration_ms: int = 0,
        result_chars: int = 0,
        token_id: str | None = None,
        delivered: bool = False,
        detail: str = "",
    ) -> dict[str, Any]:
        """Append a run record, evicting the oldest beyond :data:`MAX_RUNS`."""
        if outcome not in VALID_OUTCOMES:
            raise WebhookError(f"unknown outcome: {outcome}")
        record = {
            "id": "run_" + secrets.token_hex(6),
            "hook_id": hook_id,
            "session_key": session_key,
            "name": name,
            "outcome": outcome,
            "started_at": float(started_at if started_at is not None else time.time()),
            "duration_ms": int(duration_ms),
            "result_chars": int(result_chars),
            "token_id": token_id,
            "delivered": bool(delivered),
            "detail": detail,
        }
        path = self.path
        try:
            with locked(path):
                runs = self._load()
                runs.insert(0, record)
                write_json_atomic(path, {"runs": runs[:MAX_RUNS]})
        except (OSError, WebhookStoreUnreadable):
            # History is diagnostics — never fail a webhook run over it. That
            # covers an unreadable history file as well as a failed write: the
            # read now refuses rather than reporting an empty store, and letting
            # that refusal escape would turn rejection responses into 500s and
            # drop completed turn output on the delivery path. The file is left
            # untouched either way, which is the point of the refusal.
            logger.warning("webhook run history write failed", exc_info=True)
        return record


# ── Accessors (constructed per call so tests can repoint config_dir) ──


def token_store() -> WebhookTokenStore:
    return WebhookTokenStore()


def run_store() -> WebhookRunStore:
    return WebhookRunStore()


# ── Failed-auth throttle ──
#
# ``/api/hooks/agent`` is reachable by external callers (it sits on the
# token_auth bypass list because it authenticates itself), so an unauthenticated
# attempt costs an attacker nothing. A valid token authorizes a real agent turn
# with full tool access, so repeated failures from one source are slowed down
# rather than answered at line rate.
#
# Deliberately in-memory and per-process: this is an abuse damper, not a
# security boundary — the boundary is the 190-bit token compared with
# hmac.compare_digest. Persisting it would add a disk write to every rejected
# request, which is the opposite of what a flood needs.
_AUTH_FAIL_LIMIT = 10          # failures per source within the window
_AUTH_FAIL_WINDOW = 60.0       # seconds
_AUTH_FAIL_BLOCK = 300.0       # how long a tripped source stays blocked
_AUTH_FAIL_MAX_SOURCES = 1024  # bound the dict so a spoofed-IP flood cannot grow it

# source -> (failure count, window start, blocked-until)
_auth_failures: dict[str, tuple[int, float, float]] = {}


def auth_throttle_blocked(source: str, now: float | None = None) -> bool:
    """True when *source* has failed auth too often and is still in its block."""
    now = time.time() if now is None else now
    entry = _auth_failures.get(source)
    if not entry:
        return False
    _count, _started, blocked_until = entry
    return blocked_until > now


def record_auth_failure(source: str, now: float | None = None) -> bool:
    """Record one failed auth for *source*; return True if it is now blocked."""
    now = time.time() if now is None else now
    if len(_auth_failures) >= _AUTH_FAIL_MAX_SOURCES:
        # Drop entries whose window and block have both lapsed. If everything is
        # live, drop the oldest window so the dict stays bounded either way.
        stale = [
            k for k, (_c, started, blocked) in _auth_failures.items()
            if blocked <= now and started + _AUTH_FAIL_WINDOW <= now
        ]
        for k in stale:
            _auth_failures.pop(k, None)
        if len(_auth_failures) >= _AUTH_FAIL_MAX_SOURCES:
            oldest = min(_auth_failures, key=lambda k: _auth_failures[k][1])
            _auth_failures.pop(oldest, None)

    count, started, blocked_until = _auth_failures.get(source, (0, now, 0.0))
    if now - started > _AUTH_FAIL_WINDOW:
        count, started = 0, now  # window rolled over
    count += 1
    if count >= _AUTH_FAIL_LIMIT:
        blocked_until = now + _AUTH_FAIL_BLOCK
        count, started = 0, now
    _auth_failures[source] = (count, started, blocked_until)
    return blocked_until > now


def record_auth_success(source: str) -> None:
    """Clear a source's failure state after a valid token authenticates."""
    _auth_failures.pop(source, None)


def _reset_auth_throttle() -> None:
    """Test hook — the throttle is process-global module state."""
    _auth_failures.clear()


# ── Context freshness (single source of truth) ──


def context_freshness(registered_at: float | None, now: float | None = None) -> str:
    """Classify a registered context's age into a freshness tier.

    ``fresh`` (<= 1h) is injected verbatim, ``stale`` (<= 24h) is injected
    with a staleness banner, ``expired`` (> 24h, or unknown age) is
    dropped entirely. Callers must not re-derive these thresholds — the
    UI badge and the injection path share this function so they cannot
    disagree.
    """
    if not registered_at:
        return FRESHNESS_EXPIRED
    age = (now if now is not None else time.time()) - float(registered_at)
    if age < 0:
        age = 0.0  # clock skew / future stamp — treat as brand new
    if age > STALE_MAX_SECONDS:
        return FRESHNESS_EXPIRED
    if age > FRESH_MAX_SECONDS:
        return FRESHNESS_STALE
    return FRESHNESS_FRESH


def resolve_context(entry: Any, now: float | None = None) -> tuple[str, str]:
    """Return ``(freshness, injectable_text)`` for a ``hooks.json`` entry.

    *entry* is the raw JSON value; anything that is not a dict with a
    non-empty summary resolves to ``(expired, "")``. The returned text is
    exactly what the agent receives, banner included.
    """
    if not isinstance(entry, dict):
        return FRESHNESS_EXPIRED, ""
    ctx = entry.get("context_summary", "") or entry.get("summary", "")
    if not isinstance(ctx, str) or not ctx:
        return FRESHNESS_EXPIRED, ""
    try:
        registered = float(entry.get("registered_at", 0) or 0)
    except (TypeError, ValueError):
        return FRESHNESS_EXPIRED, ""
    freshness = context_freshness(registered, now)
    if freshness == FRESHNESS_EXPIRED:
        return freshness, ""
    if freshness == FRESHNESS_STALE:
        age_hours = ((now if now is not None else time.time()) - registered) / 3600
        return freshness, f"[Context from {age_hours:.0f}h ago — may be outdated]\n{ctx}"
    return freshness, ctx
