"""Credential keys, the ``.env`` credential file and its decoder, and the dashboard port.

An owner of the :mod:`kiro_crew.config.loader` facade; it never imports the facade.
"""

from __future__ import annotations

import codecs as _codecs
import locale as _locale
import logging
import os
import re as _re
import threading
from collections.abc import Iterable, Mapping, MutableMapping
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir
from kiro_crew.config.sections import JiraAuthEntry

logger = logging.getLogger("kiro_crew.config.loader")


# Credential keys loaded from .env / environment
CRED_SLACK_APP_TOKEN = "SLACK_APP_TOKEN"

CRED_SLACK_BOT_TOKEN = "SLACK_BOT_TOKEN"

CRED_OWNER_ID = "KIROCREW_OWNER_ID"

CRED_WECOM_BOT_ID = "WECOM_BOT_ID"

CRED_WECOM_SECRET = "WECOM_SECRET"

CRED_TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"

CRED_DISCORD_BOT_TOKEN = "DISCORD_BOT_TOKEN"

CRED_WEBEX_BOT_TOKEN = "WEBEX_BOT_TOKEN"

CRED_MICROSOFT_APP_ID = "MICROSOFT_APP_ID"

CRED_MICROSOFT_APP_PASSWORD = "MICROSOFT_APP_PASSWORD"

CRED_MICROSOFT_APP_TENANT_ID = "MICROSOFT_APP_TENANT_ID"

CRED_WEIXIN_TOKEN = "WEIXIN_TOKEN"  # iLink bot credential from the Settings QR flow

CRED_FEISHU_APP_ID = "FEISHU_APP_ID"  # Feishu custom-app id (developer console)

CRED_FEISHU_APP_SECRET = "FEISHU_APP_SECRET"

CRED_JIRA_API_TOKEN = "JIRA_API_TOKEN"  # Jira Cloud/Server API token (resolved from .env)

CRED_WAKATIME_API_KEY = "WAKATIME_API_KEY"  # vault-only; never loaded from .env

CRED_AZURE_DEVOPS_EXT_PAT = "AZURE_DEVOPS_EXT_PAT"

CRED_BITBUCKET_EMAIL = "BITBUCKET_EMAIL"

CRED_BITBUCKET_API_TOKEN = "BITBUCKET_API_TOKEN"

MANAGED_VAULT_FIXED_CONSUMERS = {
    CRED_JIRA_API_TOKEN: "jira_api_token",
    CRED_WAKATIME_API_KEY: "wakatime_api_key",
}

# kiro-cli's OWN model credential. Unlike the gateway-owned channel tokens
# above, its rightful consumer is the agent subprocess itself (and the whoami
# identity probe), so it is deliberately NOT in sandbox._AGENT_DENIED_ENV_KEYS:
# the spawn paths re-inject it from the .env file after the Docker entrypoint
# scrubs it out of the gateway's /proc/<pid>/environ.
CRED_KIRO_API_KEY = "KIRO_API_KEY"

CREDENTIAL_KEYS = (
    CRED_SLACK_APP_TOKEN,
    CRED_SLACK_BOT_TOKEN,
    CRED_OWNER_ID,
    CRED_WECOM_BOT_ID,
    CRED_WECOM_SECRET,
    CRED_TELEGRAM_BOT_TOKEN,
    CRED_DISCORD_BOT_TOKEN,
    CRED_WEBEX_BOT_TOKEN,
    CRED_MICROSOFT_APP_ID,
    CRED_MICROSOFT_APP_PASSWORD,
    CRED_MICROSOFT_APP_TENANT_ID,
    CRED_WEIXIN_TOKEN,
    CRED_FEISHU_APP_ID,
    CRED_FEISHU_APP_SECRET,
    CRED_JIRA_API_TOKEN,
    CRED_AZURE_DEVOPS_EXT_PAT,
    CRED_BITBUCKET_EMAIL,
    CRED_BITBUCKET_API_TOKEN,
    CRED_KIRO_API_KEY,
)

# Channel secrets whose every reader resolves a ``secret://`` reference through
# :func:`resolve_env_credentials`, so their ``.env`` line may hold a reference
# into the vault instead of the token. The dashboard channel writer stores a new
# value for one of these keys in the vault, and ``kirocrew secrets import``
# migrates them. Identifiers (app ids, bot ids, the owner id) stay plaintext, and
# so does any credential a child process reads from its inherited environment
# (``KIRO_API_KEY``, ``AZURE_DEVOPS_EXT_PAT``): a child cannot resolve a reference.
VAULT_CHANNEL_CREDENTIAL_KEYS: frozenset[str] = frozenset(
    {
        CRED_SLACK_APP_TOKEN,
        CRED_SLACK_BOT_TOKEN,
        CRED_WECOM_SECRET,
        CRED_TELEGRAM_BOT_TOKEN,
        CRED_DISCORD_BOT_TOKEN,
        CRED_WEBEX_BOT_TOKEN,
        CRED_MICROSOFT_APP_PASSWORD,
        CRED_WEIXIN_TOKEN,
        CRED_FEISHU_APP_SECRET,
    }
)


def vault_channel_env_value(key: str, value: str | None) -> str | None:
    """The ``.env`` value a dashboard channel save writes for *key*.

    A nonempty plaintext value for a :data:`VAULT_CHANNEL_CREDENTIAL_KEYS` key
    becomes ``secret://KEY``: the save stores the token in the vault under the
    key's own name. Every other value, a value that already is a reference, and
    a clear (``None`` or ``""``) come back unchanged.
    """
    from kiro_crew.mcp_gateway.secret_uri import SECRET_URI_PREFIX

    if key not in VAULT_CHANNEL_CREDENTIAL_KEYS or not value:
        return value
    if value.startswith(SECRET_URI_PREFIX):
        return value
    return f"{SECRET_URI_PREFIX}{key}"


# Per-host Jira tokens use a hex-encoded host suffix: JIRA_TOKEN_<HEX>.
# Only hex chars are valid — restricting the pattern prevents forged key names
# injected via multiline env values from reaching the eval-based value reader
# in the Docker entrypoint.
_JIRA_TOKEN_RE = _re.compile(r"^JIRA_TOKEN_[0-9A-Fa-f]+$")


def normalize_jira_host(host: str) -> str:
    """Normalize a Jira host exactly as auth lookup and managed-slot discovery do."""
    return host.strip().lower().removesuffix(":443")


def jira_host_token_name(host: str) -> str:
    """Return the collision-free per-host Jira token name for *host*."""
    normalized = normalize_jira_host(host)
    return f"JIRA_TOKEN_{normalized.encode().hex().upper()}"


def jira_global_token_applicable(entries: Iterable[JiraAuthEntry]) -> bool:
    """Whether the global Jira token may serve one valid configured host."""
    raw_entries = list(entries)
    return len(raw_entries) == 1 and bool(normalize_jira_host(raw_entries[0].host))


# Keys from .env that were already warned about (fire once per gateway boot).
_warned_env_keys: set[str] = set()

# .env keys already checked for a different value in the process environment
# (checked once per gateway boot, the first time a load sees the key).
_shadow_checked_env_keys: set[str] = set()


_DEFAULT_PORT = 5476

# KIROCREW_PORT is validated at CLI entry (cli.py main()).
# By the time loader.py is imported the env var is a valid int or absent.
DASHBOARD_PORT: int = int(os.environ.get("KIROCREW_PORT", _DEFAULT_PORT))


def env_path() -> Path:
    return config_dir() / ".env"


def env_fingerprint() -> tuple:
    """Cheap signature of the ``.env`` file -- changes whenever it is edited.

    The credential twin of :func:`kiro_crew.config.loader_document._config_fingerprint`,
    kept SEPARATE from it on purpose: that one is the config-document cache key,
    so folding ``.env`` into it would invalidate the parsed-``config.json`` cache
    on every unrelated credential edit. The live-config watcher tracks this one
    on its own, alongside the document fingerprint, so a hand edit of ``.env`` --
    which never calls ``notify_config_written`` and is not a ``config.json``
    change -- still wakes the reload cycle.

    Includes device + inode and the change time on top of mtime, size and mode,
    for the same reason the document fingerprint does: a dashboard credential
    save lands through an atomic rename, which on a coarse-timestamp filesystem
    can keep the same mtime and size. A missing file contributes a sentinel so
    creating or deleting ``.env`` also busts the signature.
    """
    p = env_path()
    try:
        st = p.stat()
    except OSError:
        return (str(p), None)
    return (
        str(p),
        st.st_dev,
        st.st_ino,
        st.st_ctime_ns,
        st.st_mtime_ns,
        st.st_size,
        st.st_mode,
    )


def enforce_env_permissions(ep: Path) -> None:
    """Narrow ``.env`` to owner-only (POSIX) when group/other can read it.

    The single home for the credential-file permission guard that both
    :meth:`KiroCrewConfig.load_credentials` and the live watcher's off-loop
    reader run before reading. POSIX only: on Windows mode bits are meaningless
    (a chmod there toggles the read-only attribute and succeeds without
    narrowing who can read), and the owner-only lockdown
    (``platform_compat.restrict_to_owner``) is applied where the file is WRITTEN
    -- the setup wizard and the dashboard credential writers -- not on a read
    path, because a reader has no business rewriting a descriptor it did not
    create. A failure to chmod warns rather than raising, so a credential read
    on a file the process cannot re-mode still proceeds.
    """
    try:
        if platform_compat.IS_POSIX and ep.stat().st_mode & 0o077:
            ep.chmod(0o600)
    except FileNotFoundError:
        # No file, nothing to enforce. The watcher's reader calls this on every
        # forced cycle, so a home with no ``.env`` must not log a misleading
        # "cannot enforce permissions" warning for a file that does not exist.
        pass
    except OSError:
        logger.warning("Cannot enforce permissions on %s", ep)


def parse_env_lines(text: str) -> dict[str, str]:
    """The ``KEY=VALUE`` map of a ``.env`` body, values exactly as written.

    One ``KEY=VALUE`` per line, ``#`` comment lines skipped, no quotes required,
    last occurrence of a key wins. The one line parser the credential readers
    share -- the boot load (:meth:`KiroCrewConfig.load_credentials`), the
    per-key read (:func:`read_env_file_credential`) and the live watcher
    (:func:`read_env_credentials_raw`) -- so they agree on what the file says.
    Values are the literal text, before vault resolution.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def read_env_credentials_raw(*, definitive: list[bool] | None = None) -> dict[str, str]:
    """The ``KEY=VALUE`` map of the data home's ``.env``, values exactly as written.

    The LITERAL line values, before vault resolution -- a ``secret://NAME``
    reference comes back as that reference, not the secret it names. This is
    what the live watcher diffs across reloads to find which credentials an
    operator changed, and what it writes into ``os.environ`` for a changed key,
    so the process environment carries exactly what a gateway restart would
    propagate (the reference, resolved per-reader by ``resolve_env_credentials``).

    Enforces the credential-file permission guard (:func:`enforce_env_permissions`)
    before reading, the same as :meth:`KiroCrewConfig.load_credentials`: the
    watcher's off-loop read must not be a weaker path to the file than boot's.

    ``definitive``, when given, receives ONE bool: ``True`` when the file was
    decoded or is absent (so an empty map means the keys really are gone), and
    ``False`` when it could not be read, is UTF-16/UTF-32, or has a byte that is
    not valid in its encoding (so an empty map proves nothing -- a caller
    diffing credentials must NOT read it as every key cleared, or an unreadable
    save would unset every credential and close its channels). An absent file
    is definitive-and-empty, matching boot.
    """
    ep = env_path()
    enforce_env_permissions(ep)
    try:
        text = read_env_text(ep)
    except FileNotFoundError:
        if definitive is not None:
            definitive.append(True)
        return {}
    except EnvFileWideEncodingError as exc:
        warn_undecodable_env(ep, exc)
        if definitive is not None:
            definitive.append(False)
        return {}
    except UnicodeDecodeError as exc:
        # A byte that is not valid in the chosen encoding (no BOM involved) --
        # e.g. a Latin-1 byte under a UTF-8 locale. Same treatment as the wide-
        # encoding case: warn once, treat as non-definitive. Left uncaught, this
        # would propagate out of the live watcher's poll cycle and, because
        # ``env_changed`` stays true and the ``.env`` fingerprint is advanced
        # further down (never reached when this raises), every later tick would
        # hit the same decode error and the config-reload path would starve
        # alongside it.
        warn_undecodable_env(ep, exc)
        if definitive is not None:
            definitive.append(False)
        return {}
    except OSError:
        if definitive is not None:
            definitive.append(False)
        return {}
    if definitive is not None:
        definitive.append(True)
    return parse_env_lines(text)


# The raw ``.env`` map (literal line values) as the gateway's FIRST
# ``load_credentials()`` of this process read it -- the credential baseline the
# live watcher must diff against so a ``.env`` rotation made in the window
# between that boot read and the watcher's post-bind start is detected and
# applied, not silently adopted. Recorded once (the boot load), never
# overwritten by a later dashboard/doctor ``load_credentials()`` whose disk read
# would already reflect such a window edit and so erase the very difference the
# watcher must see. ``None`` until the first load; consumed once by the watcher.
_boot_raw_env: dict[str, str] | None = None
# The ``.env`` path that record was read from (``None`` when the recorder did
# not say), so a consumer resolving a DIFFERENT data home never adopts it.
_boot_raw_env_path: str | None = None
_boot_raw_env_lock = threading.Lock()


def record_boot_raw_env(
    raw: Mapping[str, str], *, path: "os.PathLike[str] | str | None" = None
) -> None:
    """Record the raw ``.env`` map the FIRST ``load_credentials()`` parsed.

    Only the first call of the process wins: that is the gateway boot read, the
    one whose values were propagated into ``os.environ`` via ``setdefault``.
    Every later ``load_credentials()`` (a dashboard or doctor read) is ignored,
    because its disk read may already reflect a boot-window ``.env`` edit and
    would overwrite the baseline with the post-edit value -- hiding the change
    the watcher exists to apply. A copy is stored so a later mutation of the
    caller's dict cannot move the baseline. *path* is the ``.env`` file the map
    was read from; :func:`take_boot_raw_env` refuses the record for any other.
    """
    global _boot_raw_env, _boot_raw_env_path
    with _boot_raw_env_lock:
        if _boot_raw_env is None:
            _boot_raw_env = dict(raw)
            _boot_raw_env_path = os.fspath(path) if path is not None else None


def take_boot_raw_env(*, path: "os.PathLike[str] | str | None" = None) -> dict[str, str] | None:
    """Consume the recorded boot ``.env`` map, or ``None`` if none was recorded.

    Consume-once: the watcher reads it exactly at ``start()`` to seed its diff
    baseline, and a second read returns ``None`` so nothing else can mistake a
    stale boot snapshot for current state. ``None`` means no boot load ran
    (tests that drive the watcher directly), in which case the watcher falls
    back to its own off-loop read.

    When the caller names the ``.env`` *path* it is about to watch and the record
    was read from a different file, the record is discarded and ``None`` returned:
    a map from another data home is not this watcher's baseline, and adopting it
    would make the first cycle diff that home's keys against this one and
    dispatch credential changes nothing made.
    """
    global _boot_raw_env, _boot_raw_env_path
    with _boot_raw_env_lock:
        taken, taken_path = _boot_raw_env, _boot_raw_env_path
        _boot_raw_env, _boot_raw_env_path = None, None
    if taken is None:
        return None
    if path is not None and taken_path is not None and os.fspath(path) != taken_path:
        return None
    return dict(taken)


def reset_boot_raw_env() -> None:
    """Clear the recorded boot ``.env`` map. For tests that re-drive boot."""
    global _boot_raw_env, _boot_raw_env_path
    with _boot_raw_env_lock:
        _boot_raw_env, _boot_raw_env_path = None, None


# The credential/setting keys ``.env`` propagation ACTUALLY wrote into
# ``os.environ`` -- boot keys whose ``setdefault`` found the name absent and set
# it, plus every key a live apply set. A key the process already held from its
# service/shell environment is NOT here even when ``.env`` carried the same
# value, because boot's ``setdefault`` was a no-op for it: the inherited value
# is the one in force, and ``.env`` never owned it. The live remover consults
# this instead of value-equality, so removing a ``.env`` line cannot delete an
# inherited credential that merely matched. Process-wide and lock-guarded;
# readers/writers run on worker threads.
_env_owned_keys: set[str] = set()
_env_owned_keys_lock = threading.Lock()

#: The environment marker an in-app restart uses to carry the ``.env`` ownership
#: record across ``os.execv``. The record is Python module state, which a re-exec
#: resets even though ``os.execv`` keeps ``os.environ`` -- so without this the
#: successor would find every ``.env`` key already in ``os.environ`` (carried
#: through the exec), record none as owned, and never pop one on a later ``.env``
#: removal. Every change to the record hands the current key list to
#: ``platform_compat.keep_for_reexec``, the one channel for a value only this
#: process's exec successor sees: the ``reexec_*`` seams write it into the
#: environment right before the exec, and no spawned child ever inherits it.
#: The successor adopts it on its first ``load_credentials`` and deletes it.
_ENV_OWNED_KEYS_REEXEC_MARKER = "_KIROCREW_ENV_OWNED_KEYS"

#: Separator for the serialised key list. A comma is legal in an env value (NUL
#: is NOT -- ``os.environ`` rejects it, which is why the list cannot be
#: NUL-joined) and never appears in a legal env var name (``is_legal_env_name``
#: forbids only ``=``/NUL/empty, but credential keys are ``[A-Z0-9_]`` names),
#: so splitting on it cannot tear a key.
_ENV_OWNED_KEYS_SEP = ","


def _keep_env_owned_keys_for_reexec_locked() -> None:
    """Hand the current ownership record to this process's exec successor.

    Called with ``_env_owned_keys_lock`` held, after every change to the record,
    so whatever the record holds when a ``reexec_*`` seam runs is what the
    successor adopts. An empty record still keeps an empty marker, so the
    successor can tell "re-exec with nothing owned" from "fresh boot, no marker"
    and not fall back to re-deriving ownership from an ambiguous environment.
    """
    platform_compat.keep_for_reexec(
        _ENV_OWNED_KEYS_REEXEC_MARKER, _ENV_OWNED_KEYS_SEP.join(sorted(_env_owned_keys))
    )


def note_env_owned_key(key: str) -> None:
    """Record that ``.env`` propagation wrote *key* into ``os.environ``.

    Stored by canonical entry key (case-folded on Windows), so ownership tracks
    the ``os.environ`` ENTRY rather than the exact case ``.env`` happened to
    use -- a later removal under a different case still recognises its own entry.
    """
    with _env_owned_keys_lock:
        _env_owned_keys.add(_env_name_key(key))
        _keep_env_owned_keys_for_reexec_locked()


def discard_env_owned_key(key: str) -> None:
    """Drop *key* from the set of values ``.env`` owns (a removed or inherited key)."""
    with _env_owned_keys_lock:
        _env_owned_keys.discard(_env_name_key(key))
        _keep_env_owned_keys_for_reexec_locked()


def env_owns_key(key: str) -> bool:
    """Whether ``.env`` propagation is what put *key* in ``os.environ``.

    Matched by canonical entry key (case-folded on Windows), so a removal under a
    different case than the write still recognises the entry ``.env`` owns.
    """
    with _env_owned_keys_lock:
        return _env_name_key(key) in _env_owned_keys


def reset_env_owned_keys() -> None:
    """Clear the ``.env`` ownership record. For tests, and reset between boots."""
    with _env_owned_keys_lock:
        _env_owned_keys.clear()
        _keep_env_owned_keys_for_reexec_locked()


def adopt_env_owned_keys_from_reexec() -> bool:
    """Adopt a re-exec ownership marker into the record, if one is present.

    Reads and DELETES the marker so no spawned child inherits it. Returns True
    when a marker was present (the successor of an in-app restart), False on a
    fresh boot. On True the ownership record is set to exactly what the marker
    names, so boot's propagate loop then only ADDS keys genuinely new to this
    successor rather than re-deriving ownership from ``os.environ`` it cannot
    read unambiguously. Either way the record is handed on for this process's
    own exec successor, so a restart before any key is owned still carries an
    (empty) marker.
    """
    marker = os.environ.pop(_ENV_OWNED_KEYS_REEXEC_MARKER, None)
    with _env_owned_keys_lock:
        if marker is not None:
            _env_owned_keys.clear()
            _env_owned_keys.update(k for k in marker.split(_ENV_OWNED_KEYS_SEP) if k)
        _keep_env_owned_keys_for_reexec_locked()
    return marker is not None


def _effective_entry_values(env: Mapping[str, str]) -> dict[str, str]:
    """Each ``os.environ`` entry's value as boot would propagate it from *env*.

    The first non-empty spelling in ``.env`` order wins, keyed by canonical entry
    name -- boot's ``setdefault`` precedence, and the value
    :func:`apply_env_credential_changes` installs. On POSIX every name is its
    own entry, so this is just the map's non-empty values.
    """
    out: dict[str, str] = {}
    for name, value in env.items():
        if value:
            out.setdefault(_env_name_key(name), value)
    return out


def changed_env_keys(old: Mapping[str, str], new: Mapping[str, str]) -> frozenset[str]:
    """Keys whose ``.env`` change can alter the process environment.

    Every key whose value differs between *old* and *new* (added, removed or
    edited), PLUS every spelling of an ``os.environ`` entry whose effective value
    changed. The second set matters on Windows, where ``os.environ`` is
    case-insensitive and several spellings share one entry: a save that only
    REORDERS two spellings holding different values changes no key's value, yet
    changes which one boot (and the live applier) installs. Missing it would
    advance the watcher's baseline without applying the new winner. On POSIX an
    entry's effective value changes only when its one key's value does, so the
    result there is exactly the per-key set.
    """
    changed = {k for k in set(old) | set(new) if old.get(k, None) != new.get(k, None)}
    old_eff, new_eff = _effective_entry_values(old), _effective_entry_values(new)
    moved = {c for c in set(old_eff) | set(new_eff) if old_eff.get(c) != new_eff.get(c)}
    if moved:
        changed.update(k for k in (*old, *new) if _env_name_key(k) in moved)
    return frozenset(changed)


def _env_name_key(name: str) -> str:
    """The canonical form two names share iff they are the SAME ``os.environ`` entry.

    POSIX environment names are case-sensitive, so the name is its own key.
    Windows ``os.environ`` is case-INsensitive (``HTTPS_PROXY`` and
    ``https_proxy`` are one entry), so there the key is the upper-cased name.
    Used so a case-only ``.env`` rename -- which the diff sees as one key added
    and another removed -- does not let the removal pop the entry the addition
    just wrote on Windows.
    """
    return name.upper() if platform_compat.IS_WINDOWS else name


def is_legal_env_name(key: str) -> bool:
    """Whether *key* is a name ``os.environ[key] = ...`` accepts.

    ``putenv`` rejects an empty name and a name holding ``=`` or a NUL byte with
    ``ValueError``; a ``.env`` line like ``=value`` parses to the empty name and
    is exactly such a reject. The applier validates a whole credential diff with
    this BEFORE writing any of it, so one malformed key cannot leave half the
    diff applied.
    """
    return bool(key) and "=" not in key and "\x00" not in key


def apply_env_credential_changes(
    changed: Iterable[str],
    new: Mapping[str, str],
    old: Mapping[str, str] | None = None,
    *,
    path_additions: Mapping[str, tuple[str, ...]] | None = None,
) -> frozenset[str]:
    """Push the *changed* ``.env`` keys into ``os.environ``, set or pop per key.

    The live-reload counterpart of the propagate loop in
    :meth:`KiroCrewConfig.load_credentials`, but for an EDIT rather than a boot:
    a key present in *new* is set to its (literal) value and a key gone from
    *new* is popped, so a cleared or rotated credential stops reading as its old
    value for spawned children and every point-of-use ``os.environ`` read.

    It deliberately uses ``os.environ[k] = v`` / ``pop``, NOT ``setdefault``:
    ``setdefault`` lets an inherited shell value win, which is the right
    precedence at startup but the wrong one for an operator who edits ``.env``
    while the gateway runs and means that edit, so a live change overwrites.
    Only keys the operator actually changed are touched, so an inherited var the
    operator never put in ``.env`` is left alone.

    *old* is the ``.env`` map the gateway last propagated (the diff baseline).
    When supplied it makes two behaviours precise, because it is the only way to
    tell a value ``.env`` itself put in the environment from one the process
    inherited:

    * **Removal pops only what ``.env`` set.** A key dropped from *new* is popped
      ONLY when the ownership record says ``.env`` propagation actually wrote it
      (:func:`env_owns_key`), NOT by comparing values. If the process holds a
      value ``.env`` never wrote (a shell export, a service-unit
      ``Environment=``) -- even one that happens to EQUAL the ``.env`` line --
      ``.env`` does not own it, so it is left in place rather than unsetting it
      and leaving a channel uncredentialed until the next restart. (Boot records
      ownership only when its ``setdefault`` found the name absent; a live set
      records it too.)
    * **The overwrite warning fires only on a genuine collision.** A rotation
      (``old[k]`` was propagated, now a new value) does not warn, because the
      environment held exactly what ``.env`` last put there. The warning fires
      only when the current value matches NEITHER the new nor the old ``.env``
      value -- i.e. a foreign/inherited value is being overwritten, the one case
      where a live change diverges from what a restart would rebuild.

    Without *old* (a caller with no baseline) a key in *new* is set and a key
    absent from *new* is popped unconditionally, and the warning is suppressed
    rather than fired on every write.

    The two carve-outs the boot loop makes are kept, for every caller:

    * ``_KIROCREW_CREDS_SCRUBBED`` -- when the Docker entrypoint scrubbed
      credentials out of the gateway environ, re-injecting a credential key here
      would re-expose it in ``/proc/<pid>/environ``, the exact surface the
      entrypoint closed. A changed credential key is applied to nothing in that
      mode (children read it from their own ``.env`` or an explicit ``env=``);
      a non-credential operator setting still applies.
    * ``PATH`` and other prepend-style keys extend the process PATH rather than
      replacing it, the same as boot.

    *path_additions* lets the live reloader hand in PATH directories it already
    expanded and validated OFF the event loop (keyed by the PATH key name, e.g.
    ``{"PATH": (...)}``). Expanding a ``~otheruser`` entry calls ``pwd.getpwnam``
    / the SAM database synchronously, which an LDAP/SSSD/AD lookup can stall; the
    live applier runs on the loop, so it prepares the additions in a worker
    thread (:func:`kiro_crew.env.prepare_env_path_additions`) and passes them
    here, leaving only the ``os.environ["PATH"]`` mutation on the loop. When a
    PATH key is absent from this map (boot, the off-loop dashboard writer, tests)
    the expansion runs inline as before.

    The whole batch is validated BEFORE any mutation: a changed key whose *new*
    value is non-empty (so it would be written to ``os.environ`` or extend the
    PATH) must be a legal environment name AND carry a value with no embedded NUL
    -- both are things ``os.environ[k] = v`` rejects with ``ValueError``. If any
    is not (a malformed ``.env`` line like ``=value`` parses to the empty name; a
    hand-typed NUL lands in a value), the function raises ``ValueError`` and
    touches nothing, rather than setting the keys iterated before the bad one and
    leaving ``os.environ`` half-mutated. A half-mutation is not undone by the
    caller keeping its baseline: a later restore of the old file would diff to
    nothing and the stray process value would outlive the repair. Pops are never
    validated -- removing a key cannot raise -- but an illegal key or value
    appearing in the diff at all means the file is malformed, so the whole apply
    is refused and retried once the operator fixes it.

    Returns the keys it actually applied (for logging). Values are never logged.
    """
    scrubbed = bool(os.environ.get("_KIROCREW_CREDS_SCRUBBED"))
    changed = list(changed)
    # Validate first: any key that would be WRITTEN (non-empty new value, and not
    # skipped by the Docker scrub) must be a legal env name and a NUL-free value.
    # Reject the whole batch before mutating anything, so a malformed key OR value
    # never leaves a partial apply behind. Scrub-skipped credential keys are not
    # written here, so they need no validation.
    for k in changed:
        if scrubbed and (k in CREDENTIAL_KEYS or _JIRA_TOKEN_RE.match(k)):
            continue
        v = new.get(k)
        if k in new and v:
            if not is_legal_env_name(k):
                raise ValueError("illegal environment variable name in .env credential diff")
            if "\x00" in v:
                raise ValueError("embedded NUL in .env credential value")
    # Decide per os.environ ENTRY, not per ``.env`` key. On POSIX names are
    # case-sensitive, so every entry has exactly one spelling and this is the
    # plain per-key apply. On Windows ``os.environ`` is case-insensitive:
    # ``HTTPS_PROXY`` and ``https_proxy`` are ONE entry, and a per-key loop
    # writes, pops and owns that entry once per spelling -- so a rename, a
    # duplicate pair with one spelling removed, or both spellings edited in one
    # save each left the entry depending on which spelling the loop met last.
    # Grouping the batch by canonical entry name and resolving each entry ONCE
    # removes that order dependence:
    #
    # * the entry's desired value is the FIRST non-empty spelling in ``.env``
    #   order -- the one boot's ``setdefault`` propagation installs (``new``
    #   keeps the file's order) -- so a live apply lands where a restart would;
    # * an entry with a surviving spelling is set to that value; an entry with
    #   none left is removed (subject to ownership, below);
    # * spellings the diff did not touch still count as survivors, because the
    #   entry is judged against all of ``new``, not just the changed keys.
    # canonical entry -> (the spelling boot would propagate, its value)
    desired: dict[str, tuple[str, str]] = {}
    for name, value in new.items():
        if value:
            desired.setdefault(_env_name_key(name), (name, value))
    previous: dict[str, str] = {}
    if old is not None:
        for name, value in old.items():
            if value:
                previous.setdefault(_env_name_key(name), value)

    applied: set[str] = set()
    entries: dict[str, list[str]] = {}
    for k in changed:
        if scrubbed and (k in CREDENTIAL_KEYS or _JIRA_TOKEN_RE.match(k)):
            continue
        if platform_compat.env_key_allowed(k, ("PATH",)):
            # PATH is always present, so there is nothing to clear; a new value
            # extends it exactly as boot's propagate loop does. When the caller
            # pre-expanded the additions off the event loop, use those so the
            # blocking ``~otheruser`` lookup never runs on the loop; otherwise
            # expand inline (boot / off-loop dashboard / tests).
            val = new.get(k)
            if val:
                from kiro_crew.env import extend_process_path

                prepared = path_additions.get(k) if path_additions is not None else None
                extend_process_path(val, prepared=prepared)
                applied.add(k)
            continue
        entries.setdefault(_env_name_key(k), []).append(k)

    for canon, spellings in entries.items():
        # Any spelling addresses the same entry; reading/writing through the
        # first changed one is enough (on POSIX it is the only one).
        k = spellings[0]
        target = desired.get(canon)
        if target is not None:
            # Write through the spelling boot would propagate. On Windows every
            # spelling addresses the same entry, so this only fixes which name
            # the log and the write use; on POSIX it is the changed key itself.
            k, value = target
        prior = os.environ.get(k)
        if target is not None:
            prev_env_value = previous.get(canon)
            if old is not None and prior is not None and prior not in (value, prev_env_value):
                # The process holds a value that is neither the new nor the last
                # propagated ``.env`` value: a foreign/inherited value is being
                # overwritten. That is the one case worth surfacing (a plain
                # rotation is not), and a restart would rebuild the inherited
                # value, so the live change will not survive one. Key name only.
                logger.warning(
                    "%s in .env overwrites a value in the process environment "
                    "that .env did not set (an inherited shell or service value); "
                    "this applies live but a gateway restart reverts to the "
                    "inherited value until it is removed (key name only; value "
                    "not logged).",
                    k,
                )
            # A value ``.env`` merely MATCHED is not one it took over: the
            # process already held exactly this value from somewhere ``.env``
            # does not own (a service unit or shell export), so the write below
            # changes nothing. Recording ownership there would let a later
            # removal pop the inherited credential -- the very case boot's
            # setdefault refuses to own. Any write that does change the entry (a
            # new key, a rotation, an overwrite of a foreign value, or a switch
            # to another surviving spelling's value), or one ``.env`` already
            # owns, is a genuine ``.env`` write and owns it.
            merely_matched = prior is not None and prior == value and not env_owns_key(k)
            os.environ[k] = value
            if not merely_matched:
                note_env_owned_key(k)
            applied.update(spellings)
            continue
        # No spelling of this entry is left in ``.env``. Pop only what ``.env``
        # itself PROPAGATED: ownership is recorded at propagation time, NOT
        # inferred from value equality, so a service/shell value ``.env`` merely
        # matched is left in place and a removal cannot strand a channel by
        # deleting an inherited credential that happened to equal the line.
        # Without a baseline (the dashboard path, no ownership tracking) pop
        # unconditionally, the pre-baseline behaviour.
        if old is None or prior is None or env_owns_key(k):
            os.environ.pop(k, None)
            applied.update(spellings)
        # The entry left ``.env``, so drop any ownership claim on it.
        discard_env_owned_key(k)
    return frozenset(applied)


# ``.env`` paths already warned about as undecodable. The set exists because
# the gateway re-reads ``.env`` from many call sites over its whole life
# (every ``read_env_file_credential``), and one bad file must not log on each
# read. A path leaves the set when :func:`read_env_file` next decodes it:
# without that, the first warning would mute the path for the rest of the
# process, and an operator who fixes the file and later breaks it again (a
# second editor save in UTF-16) would get no warning at all while every
# credential silently reads as unset.
_warned_undecodable_env: set[str] = set()


class EnvFileWideEncodingError(UnicodeDecodeError):
    """A ``.env`` starts with a UTF-16 or UTF-32 byte-order mark.

    Readers catch exactly this to treat the file as unset. Any other decode
    error (a BOM-less file invalid in the chosen encoding) propagates as it
    always has, so the BOM handling cannot hide an unrelated bad file.

    ``encoding`` is the codec the mark declares (``"utf-32-le"`` and so on)
    and ``object`` holds only the mark itself, never the credentials after
    it. :attr:`wide_encoding` names the family for a message.
    """

    @property
    def wide_encoding(self) -> str:
        """``"UTF-16"`` or ``"UTF-32"``."""
        return _wide_family(self.encoding)

    def __str__(self) -> str:
        # The base class would say "'utf-16-le' codec can't decode bytes in
        # position 0-1", which reads as a decoding bug rather than a file
        # saved in the wrong encoding.
        return f"the .env is saved as {self.wide_encoding}; re-save it as UTF-8"


# Longest mark first: a UTF-32LE mark (FF FE 00 00) begins with the UTF-16LE
# one (FF FE), so it must be matched before it.
_WIDE_BOMS = (
    (_codecs.BOM_UTF32_LE, "utf-32-le"),
    (_codecs.BOM_UTF32_BE, "utf-32-be"),
    (_codecs.BOM_UTF16_LE, "utf-16-le"),
    (_codecs.BOM_UTF16_BE, "utf-16-be"),
)


def _wide_family(codec: str) -> str:
    return "UTF-32" if codec.startswith("utf-32") else "UTF-16"


def _wide_bom(head: bytes) -> tuple[bytes, str] | None:
    """The wide byte-order mark *head* starts with and the codec it declares."""
    for bom, codec in _WIDE_BOMS:
        if head.startswith(bom):
            return bom, codec
    return None


def env_bom_prefix(ep: Path) -> str:
    """The UTF-8 byte-order mark to put back when rewriting *ep*, or ``""``.

    :func:`read_env_text` drops a UTF-8 BOM, and an in-place rewriter that
    wrote the text back without it would change how the file decodes: with
    the BOM it is UTF-8, without it a bare read falls back to the locale, so
    a non-ASCII byte that loaded before the save would fail or turn to
    mojibake after it. Each rewriter prefixes this to the text it writes.
    Reads only the first three bytes; ``""`` when the file is absent or
    unreadable.
    """
    try:
        with ep.open("rb") as fh:
            head = fh.read(len(_codecs.BOM_UTF8))
    except OSError:
        return ""
    return "\ufeff" if head == _codecs.BOM_UTF8 else ""


def decode_env_bytes(raw: bytes, encoding: str | None = None, errors: str = "strict") -> str:
    """Decode the bytes of a ``.env`` file, honouring a byte-order mark.

    A UTF-8 BOM (what PowerShell 5.1's ``Out-File -Encoding utf8`` writes) says
    the file is UTF-8, so the rest is decoded as UTF-8 and the BOM is dropped:
    left in, it becomes part of the first key and that credential silently
    never matches. A UTF-16 BOM (PowerShell's default ``>`` / ``Out-File``) or
    a UTF-32 one raises :class:`EnvFileWideEncodingError`, a
    :class:`UnicodeDecodeError` readers can tell apart: a wide-encoded
    credential file is not a supported format, and decoding it as UTF-8 or the
    locale would either fail with an unrelated error or, under a single-byte
    code page such as cp1252, silently yield NUL-riddled keys. Without a BOM
    the bytes are decoded exactly as before: with *encoding*, or the locale
    default that a bare ``read_text()`` uses when *encoding* is ``None``.
    """
    if raw.startswith(_codecs.BOM_UTF8):
        return raw[len(_codecs.BOM_UTF8) :].decode("utf-8", errors)
    wide = _wide_bom(raw)
    if wide is not None:
        bom, codec = wide
        # Carry only the BOM bytes: ``.object`` is shown by repr() and by a
        # traceback that captures locals, and the rest of the file is secrets.
        reason = f"{_wide_family(codec)} byte-order mark"
        raise EnvFileWideEncodingError(codec, bom, 0, len(bom), reason)
    return raw.decode(encoding or _locale.getpreferredencoding(False), errors)


def read_env_file(
    ep: Path, encoding: str | None = None, errors: str = "strict"
) -> tuple[bytes, str]:
    """Read a ``.env`` file once; return its raw bytes and decoded text.

    Every reader and in-place rewriter of the data home's ``.env`` decodes it
    here, or through :func:`read_env_text`, so they agree on what the first
    key is: if only the readers stripped a BOM, a dashboard clear could not
    match the key the gateway loads, and the cleared credential would survive.
    The bytes are for a rewriter that must compare against or restore exactly
    what it parsed (the secrets migration); they come from the same read as
    the text. Raises :class:`OSError` or :class:`UnicodeDecodeError`; a
    rewriter lets the decode error propagate so it never overwrites a file it
    could not parse. A successful decode re-arms :func:`warn_undecodable_env`
    for *ep*.
    """
    raw = ep.read_bytes()
    text = decode_env_bytes(raw, encoding, errors)
    _warned_undecodable_env.discard(str(ep))
    return raw, text


def read_env_text(ep: Path, encoding: str | None = None) -> str:
    """The decoded text of a ``.env`` file; see :func:`read_env_file`."""
    return read_env_file(ep, encoding)[1]


def undecodable_env_message(ep: Path, exc: UnicodeDecodeError) -> str:
    """The one sentence that tells an operator *ep* is unreadable and how to fix it.

    The gateway logs it through :func:`warn_undecodable_env`, and ``service
    install`` / ``doctor`` print it where the shell cannot see that log, so every
    surface names the same file and the same re-save.
    """
    return f"Cannot decode {ep} ({exc.reason}); treating it as empty. Save it as UTF-8."


def warn_undecodable_env(ep: Path, exc: UnicodeDecodeError) -> None:
    """Log that *ep* could not be decoded and is treated as unset.

    Once per path until :func:`read_env_file` next decodes it.
    """
    key = str(ep)
    if key in _warned_undecodable_env:
        return
    _warned_undecodable_env.add(key)
    logger.warning("%s", undecodable_env_message(ep, exc))


def read_env_file_credential(
    key: str, env_file: Path | None = None, *, definitive: list[bool] | None = None
) -> str:
    """Best-effort read of one ``KEY=VALUE`` entry from the data home's ``.env``.

    Same line format :meth:`KiroCrewConfig.load_credentials` parses (one pair
    per line, ``#`` comments, no quotes required, last occurrence wins).
    Returns ``""`` when the file is absent, unreadable or UTF-16/UTF-32 encoded —
    callers treat the credential as unset rather than failing.

    ``definitive``, when given, receives ONE bool from this same read: True when
    the file was decoded or does not exist, so an empty answer means the key is
    not there; False when it could not be read or is UTF-16/UTF-32, so an empty
    answer proves nothing. Any other decode error propagates, as before, and
    records nothing.

    Blocking file IO: call via ``asyncio.to_thread`` from async paths.
    """
    ep = env_file if env_file is not None else env_path()
    try:
        text = read_env_text(ep)
    except FileNotFoundError:
        if definitive is not None:
            definitive.append(True)
        return ""
    except EnvFileWideEncodingError as exc:
        warn_undecodable_env(ep, exc)
        if definitive is not None:
            definitive.append(False)
        return ""
    except OSError:
        if definitive is not None:
            definitive.append(False)
        return ""
    if definitive is not None:
        definitive.append(True)
    # The shared parser, so this per-key read sees the file exactly as the boot
    # load and the live watcher do (last occurrence of a key wins).
    return parse_env_lines(text).get(key, "")


# Key -> the reason last logged by :func:`resolve_env_credentials`, so a
# reference the gateway re-reads on every settings request logs once, not on
# each read. A key drops out once it resolves again. Readers run on worker
# threads, so every access holds the lock.
_warned_unresolved_env_refs: dict[str, str] = {}

_warned_unresolved_env_refs_lock = threading.Lock()


def _is_env_credential_key(key: str) -> bool:
    """True for a ``.env`` key :func:`resolve_env_credentials` resolves."""
    return key in CREDENTIAL_KEYS or bool(_JIRA_TOKEN_RE.match(key))


def resolve_env_credentials(
    values: Mapping[str, str], *, home: Path | None = None
) -> dict[str, str]:
    """Resolve the ``secret://NAME`` credential values in *values* from the vault.

    The one resolver every in-process ``.env`` credential reader shares. A
    credential key (:data:`CREDENTIAL_KEYS` or a per-host ``JIRA_TOKEN_<HEX>``)
    whose value starts with ``secret://`` is replaced by the vault entry it
    names; a plaintext value, and every non-credential key, passes through
    unchanged, so a hand-edited ``.env`` keeps working as it is.

    A reference that cannot be resolved -- a malformed name, a name the vault
    does not hold, or a vault that cannot be read -- fails closed: its key is
    left out of the result (the credential reads as unset, never as the literal
    reference) and one warning names the key and a short fixed reason. No log
    line or error carries a secret value or a referenced name: only the
    allowlisted key is ever named.

    *home* is the directory holding the ``.env`` and its ``.vault``; it defaults
    to the data home. Blocking file IO when *values* holds a reference: call via
    ``asyncio.to_thread`` from async paths.
    """
    from kiro_crew.mcp_gateway.secret_uri import SECRET_URI_PREFIX, _is_valid_secret_name

    resolved: dict[str, str] = {}
    failures: dict[str, str] = {}
    pending: dict[str, str] = {}
    for key, value in values.items():
        if not (value.startswith(SECRET_URI_PREFIX) and _is_env_credential_key(key)):
            resolved[key] = value
            continue
        name = value[len(SECRET_URI_PREFIX) :]
        if not _is_valid_secret_name(name):
            failures[key] = "malformed secret:// reference"
            continue
        pending[key] = name
    if pending:
        from kiro_crew.secrets import SecretVault

        vault_home = home if home is not None else env_path().parent
        try:
            fetched = SecretVault(vault_home).get_many(list(pending.values()))
        except Exception:  # noqa: BLE001 - any vault fault fails closed
            fetched = None
        for key, name in pending.items():
            if fetched is None:
                failures[key] = "the secrets vault could not be read"
                continue
            secret = fetched.get(name)
            if secret is None:
                failures[key] = "the vault holds no secret by that name"
                continue
            resolved[key] = secret.reveal()
    _warn_unresolved_env_credentials(failures, resolved_keys=pending.keys() - failures.keys())
    return resolved


def _warn_unresolved_env_credentials(
    failures: Mapping[str, str], *, resolved_keys: Iterable[str] = ()
) -> None:
    """Log each unresolvable credential reference once, naming only its key."""
    to_log: list[tuple[str, str]] = []
    with _warned_unresolved_env_refs_lock:
        for key in resolved_keys:
            _warned_unresolved_env_refs.pop(key, None)
        for key, reason in sorted(failures.items()):
            if _warned_unresolved_env_refs.get(key) == reason:
                continue
            _warned_unresolved_env_refs[key] = reason
            to_log.append((key, reason))
    for key, reason in to_log:
        logger.warning(
            "%s (%s): this .env line is a vault reference that does not resolve, so it"
            " reads as unset. Store the value under Settings > Secrets, or put the"
            " plain value back on the line.",
            key,
            reason,
        )


def resolve_env_credential(key: str, value: str, *, home: Path | None = None) -> str:
    """Resolve one credential *value*; ``""`` when its reference cannot resolve.

    The single-value form of :func:`resolve_env_credentials`, for a reader that
    takes a credential from ``os.environ`` or one ``.env`` line. A plaintext
    value comes back unchanged without touching the vault.
    """
    return resolve_env_credentials({key: value}, home=home).get(key, "")


def inject_kiro_cli_api_key(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Ensure *env* carries kiro-cli's own model credential (``KIRO_API_KEY``).

    The Docker entrypoint scrubs :data:`CREDENTIAL_KEYS` out of the gateway's
    process environment into the data home's ``.env`` (mode 600) so they never
    reside in a long-lived ``/proc/<pid>/environ``. Every other credential is
    consumed in-process from :meth:`KiroCrewConfig.load_credentials`, but this
    one authenticates the kiro-cli CHILD, which reads it from its own
    environment — so kiro-cli spawn paths call this to hand the child exactly
    the one variable it owns, without re-widening the parent's environ. A value
    already present in *env* wins (same precedence as ``load_credentials``);
    outside Docker nothing changes because the variable is still inherited.

    Mutates *env* in place and returns it for convenience. Blocking file IO:
    call via ``asyncio.to_thread`` from async paths.
    """
    if not env.get(CRED_KIRO_API_KEY):
        val = read_env_file_credential(CRED_KIRO_API_KEY)
        if val:
            env[CRED_KIRO_API_KEY] = val
    return env


def strip_kiro_cli_api_key(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Remove kiro-cli's model credential from a child that does not consume it.

    Counterpart to :func:`inject_kiro_cli_api_key` for every ACP backend other
    than kiro (Claude Code, and KAS): the credential authenticates
    kiro-cli's OWN v2 agent loop, and it is deliberately NOT in
    ``sandbox._AGENT_DENIED_ENV_KEYS``, so without this an inherited copy in the
    raw ``os.environ`` snapshot would ride into an agent process that has no use
    for it.

    For KAS the child IS a kiro-cli: Crew reaches it through kiro-cli's ACP relay.
    The strip applies when Crew owns that relay's credential -- the engine asks
    Crew's vault over its ``_kiro/auth/getAccessToken`` callback, and an API key
    in its environment would take precedence over the callback. A cli-owned
    relay (``--auth-method cli``) authenticates itself, so the KAS harness hands
    it the key with :func:`inject_kiro_cli_api_key` instead -- the test is what
    the child's engine consumes, not which binary it is.

    Matches the platform env-key convention (exact on POSIX, case-folded on
    Windows) so a differently-cased Windows spelling cannot slip past. Mutates
    *env* in place and returns it.
    """
    matched = [k for k in env if platform_compat.env_key_allowed(k, _KIRO_API_KEY_ONLY)]
    for k in matched:
        del env[k]
    return env


# Single-key allowlist for strip_kiro_cli_api_key's platform-aware matching.
_KIRO_API_KEY_ONLY = frozenset({CRED_KIRO_API_KEY})
