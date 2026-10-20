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
    except OSError:
        if definitive is not None:
            definitive.append(False)
        return ""
    except EnvFileWideEncodingError as exc:
        warn_undecodable_env(ep, exc)
        if definitive is not None:
            definitive.append(False)
        return ""
    value = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip() == key:
                value = v.strip()
    if definitive is not None:
        definitive.append(True)
    return value


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
