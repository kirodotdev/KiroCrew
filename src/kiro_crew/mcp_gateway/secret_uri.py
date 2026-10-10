"""Resolve ``secret://NAME`` URIs in MCP server environment variables.

At spawn time, env values matching the ``secret://`` scheme are resolved
against the local :class:`~kiro_crew.secrets.SecretVault`. Resolution is
in-memory only — the sidecar file on disk retains the raw URI template so
the secret is never persisted in plaintext outside the vault.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.secrets import SecretVault

#: Scheme prefix for a vault secret reference. Single source of truth: the
#: importer (``kiro_crew.secrets.migrate``) writes references with this prefix
#: and the source-provider check reads it, so both import it from here rather
#: than re-spelling the literal.
SECRET_URI_PREFIX = "secret://"
_SECRET_URI_PREFIX = SECRET_URI_PREFIX  # internal alias for existing call sites

#: Validation accepts EVERY name the vault write path can store, rejecting
#: only what can never round-trip: the empty name and names with
#: leading/trailing whitespace (``api_secrets_set`` ``.strip()``s the name
#: before storing, so such a reference can never match a stored entry — see
#: ``kiro_crew.dashboard.handlers.secrets``). No character-class policy is
#: applied here: an interior space, control, format, or private-use character
#: is storable, so refusing it would strand a real vault entry and abort MCP
#: spawn for a name the product's own write path accepted. The log-injection
#: concern that motivates charset filtering is closed at the SINK instead:
#: no error or log line raised by this module ever echoes the secret name —
#: every message names only the operator-declared env-var KEY. With nothing
#: echoed, a hostile name (bidi override, newline, zero-width) has no text to
#: forge.


def _is_valid_secret_name(name: str) -> bool:
    """True if *name* could round-trip through the vault's storage boundary.

    Rejects only the empty name and leading/trailing whitespace (the store
    ``.strip()``s names, so such a reference can never match a stored entry).
    Every storable name — including interior spaces and unusual Unicode — is
    accepted; hostile characters are neutralized by never echoing names in
    errors, not by refusing to resolve them.
    """
    return bool(name) and name == name.strip()


def resolve_secret_uris(
    env: dict[str, str], config_dir: Path, *, subject: str = "MCP server"
) -> tuple[dict[str, str], set[str]]:
    """Return a copy of *env* with ``secret://NAME`` values resolved.

    Returns ``(resolved_env, secret_keys)`` where *secret_keys* is the set
    of env-var names that held a ``secret://`` URI and now contain plaintext.
    The caller MUST clear these keys from the returned dict after the child
    process has been spawned (``exec`` copies them into the child's address
    space) so that plaintext secrets do not linger in parent-process memory.

    *subject* names WHOSE env mapping is being resolved, and appears in every
    error this function raises. It exists because this resolver is not
    MCP-only: the DeepSeek Harness's ``agent.deepseek_env`` runs through it too,
    and a refusal telling that operator about an "MCP server" would send them to
    the wrong configuration surface. It never carries a secret name or value --
    see the note on :func:`_is_valid_secret_name`.

    Non-matching values pass through unchanged. Any value beginning with the
    ``secret://`` scheme is treated as a secret reference — including
    ``secret://`` with an empty or malformed name, which fails closed with a
    :exc:`ValueError` rather than passing the literal template through into the
    child's environment. Raises :exc:`ValueError` when a referenced secret does
    not exist in the vault — failing closed prevents an MCP server from
    starting with a missing credential.

    All referenced secrets are read from the vault in a single batch
    (:meth:`SecretVault.get_many`), so K references cost one store load and one
    key read rather than K of each.

    This function is intentionally synchronous: vault reads are local
    filesystem I/O and the caller (gatewayd spawn path) is already in an
    async context that would need ``await asyncio.to_thread(...)`` for a
    blocking call — keeping this sync lets the caller wrap it once.
    """
    resolved: dict[str, str] = {}
    secret_keys: set[str] = set()

    # First pass: classify each env value. Validate every secret reference's
    # name BEFORE touching the vault so a malformed URI fails closed without a
    # store read. ``pending`` maps env-var key -> secret name to resolve.
    pending: dict[str, str] = {}
    for key, value in env.items():
        if not value.startswith(_SECRET_URI_PREFIX):
            resolved[key] = value
            continue

        secret_name = value[len(_SECRET_URI_PREFIX) :]
        if not _is_valid_secret_name(secret_name):
            # Do NOT echo the raw reference: a malformed name can carry
            # control characters (CWE-117 log injection) and this ValueError
            # propagates to the spawn path's logs unsanitised. Name only the
            # env-var key, which is operator-declared config.
            raise ValueError(
                f"{subject} env var {key!r} has a malformed secret:// "
                f"reference: the name after 'secret://' must be non-empty with "
                f"no leading or trailing whitespace (stored names are "
                f"stripped, so such a reference can never match). "
                f"Fix the reference, then store the secret under "
                f"Settings > Secrets in the dashboard (or migrate it with "
                f"`kirocrew secrets import`)."
            )
        pending[key] = secret_name

    if not pending:
        return resolved, secret_keys

    vault = SecretVault(config_dir)
    fetched = vault.get_many(list(pending.values()))

    for key, secret_name in pending.items():
        secret_value = fetched.get(secret_name)
        if secret_value is None:
            raise ValueError(
                f"{subject} env var {key!r} references a secret that does not "
                f"exist in the vault (read the referenced name from the env "
                f"mapping's own entry under {key!r}). "
                f"Store it under Settings > Secrets in the dashboard (or "
                f"migrate it with `kirocrew secrets import`)."
            )
        resolved[key] = secret_value.reveal()
        secret_keys.add(key)

    return resolved, secret_keys


def header_secret_refs(headers: object) -> list[str]:
    """Names of the headers whose value carries a ``secret://`` reference.

    Only an MCP server's ``env`` is resolved against the vault. A remote server's
    ``headers`` are read by the session runtime as written, so a reference there
    reaches the server as literal text. Callers use this to say so instead of
    letting the request fail with an authorization error that names nothing.

    Accepts both shapes a header block takes in this codebase: the config mapping
    (``{"X-Api-Key": "secret://NAME"}``) and the ACP wire list
    (``[{"name": "X-Api-Key", "value": "secret://NAME"}]``). Anything else yields
    no names. Matches the prefix anywhere in the value, because a value such as
    ``Bearer secret://NAME`` is just as unresolved as a bare reference. Never
    returns a value or a secret name -- only the header names, in first-seen
    order and de-duplicated.
    """
    pairs: list[tuple[object, object]] = []
    if isinstance(headers, dict):
        pairs = list(headers.items())
    elif isinstance(headers, (list, tuple)):
        pairs = [
            (item.get("name"), item.get("value")) for item in headers if isinstance(item, dict)
        ]
    out: list[str] = []
    seen: set[str] = set()
    for name, value in pairs:
        if (
            isinstance(name, str)
            and name
            and isinstance(value, str)
            and _SECRET_URI_PREFIX in value
            and name not in seen
        ):
            seen.add(name)
            out.append(name)
    return out


#: The supported way to supply a remote server's header today, named in every
#: refusal so the reader has the fix and not only the fault. Limited to kiro-cli
#: on purpose: kiro-cli expands ``${VAR}`` / ``${env:VAR}`` in a remote server's
#: headers itself, while Crew hands the other backends their headers as written.
REMOTE_HEADER_ALTERNATIVE = (
    "On the kiro-cli backend, use ${env:NAME} in the header value and set NAME in "
    "the Kiro Crew .env file; kiro-cli expands it at session time. Other backends "
    "receive the header as written"
)

#: Bounds on the header names one refusal spells out. A header name is
#: config-derived text bound for a log line and a dashboard row.
_HEADER_NAME_CAP = 64
_HEADER_NAMES_SHOWN = 8


def display_header_names(names: list[str]) -> str:
    """Header names for a message: printable characters only, bounded, quoted."""
    shown = []
    for name in names[:_HEADER_NAMES_SHOWN]:
        clean = "".join(ch for ch in name if ch.isprintable())[:_HEADER_NAME_CAP]
        shown.append(repr(clean or "?"))
    text = ", ".join(shown)
    if len(names) > _HEADER_NAMES_SHOWN:
        text += f" (+{len(names) - _HEADER_NAMES_SHOWN} more)"
    return text


def remote_header_secret_ref_error(headers: object) -> str:
    """One sentence explaining why a remote server's header will not authenticate.

    Empty when no header carries a ``secret://`` reference. Names the headers,
    never their values or the referenced secret names.
    """
    names = header_secret_refs(headers)
    if not names:
        return ""
    noun = "Header" if len(names) == 1 else "Headers"
    return (
        f"{noun} {display_header_names(names)} use{'s' if len(names) == 1 else ''} a "
        "secret:// reference, which is not resolved for a remote server: secret "
        "references are resolved only in a stdio server's env, so the server would "
        f"receive the reference as written. {REMOTE_HEADER_ALTERNATIVE}."
    )
