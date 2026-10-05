"""Stable argv encoding and command/env hashing shared across the MCP gateway.

Kept in its own dependency-free leaf module (standard library only) so every caller
imports it at module top level. The lightweight ``rewriter`` sits on
``config.loader``'s import path, while ``pool`` and ``stub`` are asyncio/socket
-heavy submodules that must stay unloaded until the gateway is actually enabled
(``test_loader_does_not_import_mcp_gateway_at_module_load``). Routing the shared
hash through this leaf lets the rewriter import it directly without dragging
those heavy submodules into CLI/test/MCP startup.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import os
import sys
from typing import Any, Collection, Mapping, Sequence

#: One base64url JSON list carrying the stub's own flag tokens. Every raw value
#: the rewriter emits -- executable path, work dir, socket, env sidecar, server
#: and agent names, autoApprove identifiers -- rides inside it, because a CLI
#: that launches the stub through cmd.exe expands ``%NAME%`` in any plain token,
#: quoted or not, and there is no escape for it on that command line. The
#: tokens keep their plain flag spelling inside the envelope, so the stub's
#: parser and the daemon's reader see the same argv an older overlay spelled out
#: directly, and hash it identically.
STUB_FLAGS_FLAG = "--stub-flags-b64"


def encode_target_args(args: list[str]) -> str:
    """Carry argv boundaries in JSON, with a shell-inert base64url alphabet.

    Arguments may contain delimiters or be empty. Encoding also keeps their
    metacharacters out of cmd.exe's parse when a CLI launches the stub through
    a shell. This is serialization, not encryption; arguments remain visible.
    """
    payload = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_target_args(raw: str) -> list[str]:
    """Reject malformed payloads without echoing potentially sensitive arguments."""
    try:
        payload = base64.b64decode(raw.encode("ascii"), altchars=b"-_", validate=True)
        decoded = json.loads(payload.decode("utf-8"))
    except ValueError:
        raise ValueError("malformed target-args payload") from None
    if not isinstance(decoded, list) or not all(isinstance(a, str) for a in decoded):
        raise ValueError("target-args payload is not a JSON array of strings")
    return decoded


def expand_stub_flags(argv: Sequence[Any]) -> list[Any]:
    """Splice every :data:`STUB_FLAGS_FLAG` envelope in ``argv`` back into its
    plain flag tokens, in place; every other token passes through unchanged.

    Both ``--stub-flags-b64=PAYLOAD`` and ``--stub-flags-b64 PAYLOAD`` are
    read. A malformed or missing payload raises ``ValueError`` rather than
    falling back to whatever plain tokens surround it: the envelope is the only
    carrier of the values it holds, so a partial read would launch a stub
    against different metadata than the rewriter hashed. One level only -- an
    envelope inside an envelope is left as a plain token.
    """
    out: list[Any] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if isinstance(token, str) and (
            token == STUB_FLAGS_FLAG or token.startswith(STUB_FLAGS_FLAG + "=")
        ):
            if "=" in token:
                payload = token.partition("=")[2]
            else:
                i += 1
                if i >= len(argv) or not isinstance(argv[i], str):
                    raise ValueError("stub-flags envelope has no payload")
                payload = argv[i]
            out.extend(decode_target_args(payload))
        else:
            out.append(token)
        i += 1
    return out


#: Byte that opens and closes the alias encoding of a launch token (see
#: :func:`launch_token_bytes`). ``0xFF`` never occurs in UTF-8, so no literal
#: token -- and no sequence of literal tokens joined by the ``\0`` separator --
#: can produce these bytes: the encoding cannot collide with a spelled-out
#: path, so an agent cannot type a string that hashes like the gateway's own
#: interpreter.
_INSTALL_MARK = b"\xff"
_INSTALL_TAG = b"kirocrew-install"

#: Alias roles. Each names ONE file or directory of this install that the
#: gateway itself writes into a launch and that moves on every versioned
#: upgrade. Nothing else is ever folded.
_ALIAS_INTERPRETER = b"interpreter"
_ALIAS_DEPS_BOOT = b"deps-boot"
_ALIAS_SITE_PACKAGES = b"site-packages"


def _package_dir() -> str:
    """Absolute directory of the running ``kiro_crew`` package, from this file."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_IS_WINDOWS = os.name == "nt"


@functools.lru_cache(maxsize=32)
def _windows_long_path(path: str) -> str | None:
    """``GetLongPathNameW`` of one of THIS process's own paths, or ``None``.

    Windows gives every file a second spelling, the 8.3 short name
    (``C:\\PROGRA~1\\...``), and a process started through that spelling
    reports it in ``sys.executable`` and derives its ``sys.path`` -- so this
    package's directory -- from it. The rewriter launches the broker stub
    exactly that way when the install path holds a space
    (``rewriter._cmd_safe_command``), so the stub would otherwise know its own
    interpreter, shim and ``site-packages`` only by short names while the launch
    it hashes carries the gateway's long ones. The long form is the ONLY
    canonicalisation applied, it is applied only to paths computed from this
    process itself (never to a caller-supplied token), and it follows no link:
    ``GetLongPathNameW`` expands each 8.3 component to its on-disk name and
    nothing else. Unavailable (non-Windows, a path the API cannot read) folds
    to ``None`` and the alias keeps its one spelling.

    Memoized per path: the only inputs are this process's own paths, so each is
    resolved at most once per process and :func:`install_aliases` -- called per
    hashed token, sometimes on the event loop -- stays pure CPU after that.
    """
    if not _IS_WINDOWS:
        return None
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        buf = ctypes.create_unicode_buffer(32768)
        n = kernel32.GetLongPathNameW(path, buf, len(buf))
    except Exception:
        return None
    if n == 0 or n > len(buf):
        return None
    return str(buf.value)


def _own_spellings(path: str) -> tuple[str, ...]:
    """*path* as this process spells it, plus its Windows long form when distinct."""
    long = _windows_long_path(path)
    if long and long != path:
        return (path, long)
    return (path,)


def install_aliases() -> dict[str, bytes]:
    """The exact token spellings this install folds, each with its role.

    These are the strings the gateway's own code puts into a launch, computed
    here from the same sources it computes them from:

    * ``sys.executable`` -- what ``apps/bridges.py`` substitutes for a bare
      ``python3`` (``resolve_app_python``) and for the ``kirocrew`` host CLI
      (``_pin_host_cli_command``);
    * the absolute path of ``kiro_crew/apps/deps_boot.py`` -- the stdlib-only
      launch shim ``bridges.py`` inserts into argv by absolute path (``.pyc``
      spelled too, for a source-less install);
    * the directory that holds the ``kiro_crew`` package -- the
      ``site-packages`` the host-CLI pin writes into the declared ``PYTHONPATH``.

    The desktop and CLI installers lay each release out under a VERSIONED
    directory (``.../kirocrew/<version>/payload/...``), so all three spell a
    different path after an upgrade while naming the same program. Matching is
    raw-string equality: an agent-supplied token is folded only when it is
    byte-identical to a string this process computed from its own
    ``sys.executable`` and package location, so no spelling an agent can choose
    reaches a different file through the fold -- there is no containment,
    no case folding and no path parsing to get wrong. A variant spelling of the
    same file (a case change, a symlink, a mixed separator) simply hashes
    literally, which fails closed: it changes on upgrade exactly as before.

    Every process that hashes a launch -- the gateway's rewriter, gatewayd and
    the broker stub -- computes this table from ITSELF, and they agree because
    they run from one install. The one way their spellings of the same file can
    differ is Windows' 8.3 short name, which the stub is launched through when
    the install path holds a space; so on Windows each of the three also enters
    under its long name (:func:`_windows_long_path`), computed from this
    process's own path and never from a token. Nothing else is normalised.

    Rebuilt per call so a test can repoint the inputs; the only filesystem
    touch, the Windows long-name lookup, is memoized per path.
    """
    pkg = _package_dir()
    deps_boot = os.path.join(pkg, "apps", "deps_boot")
    aliases: dict[str, bytes] = {}
    if isinstance(sys.executable, str) and sys.executable:
        for spelling in _own_spellings(sys.executable):
            aliases[spelling] = _ALIAS_INTERPRETER
    for spelling in _own_spellings(deps_boot + ".py"):
        aliases[spelling] = _ALIAS_DEPS_BOOT
    for spelling in _own_spellings(deps_boot + ".pyc"):
        aliases[spelling] = _ALIAS_DEPS_BOOT
    for spelling in _own_spellings(os.path.dirname(pkg)):
        aliases[spelling] = _ALIAS_SITE_PACKAGES
    return aliases


def launch_token_bytes(token: str, role: bytes) -> bytes:
    """The bytes :func:`hash_command` folds in for one command or argv token.

    A token that is exactly the :func:`install_aliases` spelling of *role* is
    encoded as ``\\xff kirocrew-install:<role> \\xff`` -- so the hash names *the
    gateway's own interpreter / shim / package directory* rather than the
    versioned directory it lives in this release. Every other token is its UTF-8
    encoding, unchanged from before this rule existed, so a launch that names
    none of the three computes byte-for-byte the hash it always did.

    *role* is the ONE alias the caller's position may carry, and it is the
    caller -- not the token -- that names it: the command slot folds the
    interpreter, an argv slot folds the ``deps_boot`` shim, a ``PYTHONPATH``
    segment folds ``site-packages``. Those are exactly the positions the
    gateway's own writer (``apps/bridges.py``) emits the three spellings in. The
    same spelling anywhere else (the package directory as a ``--plugin-root``
    value, the interpreter as an argument, a path in some other variable) is
    content an operator or agent wrote, so it hashes literally and an upgrade
    that rewrites it is a changed launch, as before.

    Never ``realpath`` and never a prefix test: a caller-supplied token is never
    resolved (it is untrusted and a resolve can be a network probe), and a token
    either IS the gateway's own spelling for this slot or is hashed as written.
    The only filesystem read is the Windows long name of this process's OWN
    paths, done once per process (:func:`_windows_long_path`).
    """
    if install_aliases().get(token) != role:
        return token.encode("utf-8")
    return _INSTALL_MARK + _INSTALL_TAG + b":" + role + _INSTALL_MARK


def runs_install_code(command: str, args: list[str]) -> bool:
    """Whether a launch executes THIS install's own Python code.

    True when the command is the interpreter alias (:func:`install_resident`)
    AND the argv names Kiro Crew code: ``-m kiro_crew`` / ``-m kiro_crew.<mod>``
    (the host-CLI pin, a gateway module) or the ``deps_boot`` shim alias. A
    third-party app server that merely RUNS on the gateway's interpreter
    (``sys.executable server.py``) is not included: its code is the file in
    argv, which the identity already fingerprints, and re-keying it on every
    Kiro Crew release -- or on every commit of an editable install -- would
    cold-start a pool and discard a measurement for no reason.
    """
    if not install_resident(command):
        return False
    aliases = install_aliases()
    expect_module = False
    for arg in args:
        if expect_module:
            return arg == "kiro_crew" or arg.startswith("kiro_crew.")
        if arg == "-m":
            expect_module = True
            continue
        if arg.startswith("-m") and len(arg) > 2 and not arg.startswith("--"):
            module = arg[2:]
            return module == "kiro_crew" or module.startswith("kiro_crew.")
        if aliases.get(arg) == _ALIAS_DEPS_BOOT:
            return True
    return False


def install_resident(command: str) -> bool:
    """Whether *command* is the install's own interpreter alias.

    True exactly when the hash names the install rather than the versioned
    directory the interpreter lives in. :func:`runs_install_code` narrows it to
    the launches whose argv also names Kiro Crew code; those need the code
    fingerprint beside the hash (``evaluate.identity_for``,
    ``stub.pool_binary_version``), or an upgrade that replaces the code behind
    an unchanged argv would keep a stale measurement.
    """
    return install_aliases().get(command) == _ALIAS_INTERPRETER


def hash_command(command: str, args: list[str]) -> str:
    """SHA-256 over ``command\\0`` + each ``arg\\0``.

    Single source of truth for the ``command_args_hash`` dimension of
    :class:`kiro_crew.mcp_gateway.pool.PoolKey`. The stub hashes its
    ``--target-command`` + split ``--target-args`` through this to register a
    pool key; the rewriter hashes the same inputs to build the
    ``KIROCREW_MCP_TARGET_<SERVER>__<hash>`` env entry that
    ``gatewayd.env_target_resolver`` looks up by that same key; the launch
    approval store (:mod:`kiro_crew.mcp_gateway.launch_approval`) records and
    re-checks the same digest. All of them call THIS function so the
    wire-format can never drift between writer and reader.

    Each token goes through :func:`launch_token_bytes` with the one alias its
    slot may carry: the command folds the gateway's own interpreter, an argument
    folds the launch shim (:func:`install_aliases`), so an operator's approval
    of a launch that pins the gateway's interpreter (``python3`` rewritten to
    ``sys.executable``) survives the upgrade that moves that interpreter to the
    next versioned directory. The stub, gatewayd and the gateway all run from
    the same install, so they agree on the aliases and on the digest. Any other
    token -- including an alias spelling in a slot the gateway does not emit it
    in -- hashes exactly as it did before.
    """
    h = hashlib.sha256()
    h.update(launch_token_bytes(command, _ALIAS_INTERPRETER))
    h.update(b"\0")
    for a in args:
        h.update(launch_token_bytes(a, _ALIAS_DEPS_BOOT))
        h.update(b"\0")
    return h.hexdigest()


#: Env-key prefixes treated as ROTATING SECRETS and excluded from the
#: ``effective_env_hash`` PoolKey dimension, so a credential rotation does not
#: split an otherwise-identical pool.
#:
#: The exclusion has a second, security-critical consequence: it makes the hash
#: NON-INJECTIVE over these keys. Two sessions whose only difference is an
#: ``AWS_SECRET*`` value collide onto the same hash and therefore SHARE one
#: backend — so there is no single correct value for a secret-prefixed key in a
#: pooled backend, and one must never be forwarded into it. Servers that need a
#: per-session secret read it from disk (the platform credential helper / the
#: provider's default credential chain, unchanged by pooling) or stay ``poolable: false``.
#:
#: An operator can lift the exclusion for a NAMED variable via
#: ``mcp_gateway.pool_identity_env`` — see the ``identity_keys`` argument of
#: :func:`non_secret_env`. That is not a hole in the reasoning above, it is the
#: reasoning applied in reverse: naming a key makes it part of
#: ``effective_env_hash``, so the hash becomes INJECTIVE over it, two sessions
#: declaring different values no longer collide, and "no single correct value"
#: stops being true for that key. Forwarding it is then safe by exactly the
#: argument that already makes every other hashed key safe to forward.
ENV_SCRUB_PREFIXES: tuple[str, ...] = ("AWS_SECRET", "AWS_SESSION", "OAUTH")


def is_secret_env_key(key: str) -> bool:
    """Return ``True`` if ``key`` is a rotating-secret key.

    Single source of truth for the scrub decision, shared by the stub (which
    excludes these keys when hashing) and by ``gatewayd`` (which excludes them
    when forwarding declared env to a pooled backend). Sharing it is what keeps
    "every forwarded key is also a hashed key" a checkable invariant rather than
    a comment in two files.

    Forwarding applies a SECOND, independent filter on top of this one —
    ``manager.is_credential_env_key`` — so the forwarded set is a strict subset
    of the hashed set: keys the daemon's own credential scrub removes
    (``AWS_ACCESS``, ``SSH_AUTH_SOCK``, ``GNUPGHOME``, ``GIT_ASKPASS``) are in
    the hash but are still never forwarded.
    """
    return any(key.startswith(prefix) for prefix in ENV_SCRUB_PREFIXES)


def non_secret_env(
    env_pairs: Mapping[str, str], *, identity_keys: Collection[str] = ()
) -> dict[str, str]:
    """Return ``env_pairs`` minus every :func:`is_secret_env_key` entry.

    This is the set folded into :func:`hash_effective_env`, and the OUTER bound
    on what may be applied to a shared pooled backend. Because these keys are
    part of the PoolKey, every session sharing a backend agrees on their values,
    so applying them at spawn cannot make one co-tenant observe another's
    configuration.

    It is not sufficient on its own: the forwarding path in ``gatewayd`` also
    drops ``manager.is_credential_env_key`` matches, so a declared credential
    key that the daemon scrub removes is never re-introduced.

    ``identity_keys`` names variables an operator has declared pool-identity-
    relevant (``mcp_gateway.pool_identity_env``). A named key is KEPT even when
    :func:`is_secret_env_key` matches it, which folds its value into the hash and
    so restores the very property the exclusion gives up: two sessions declaring
    different values get different ``effective_env_hash`` values and therefore
    different backends. Matching is by exact name, not by prefix — the point is
    for an operator to accept the rotation-splits-the-pool cost for ONE variable,
    not to disable a whole prefix class.

    Default ``()`` is byte-for-byte today's behaviour: an installation that names
    nothing computes exactly the hash it computed before this argument existed,
    so no existing PoolKey is invalidated.
    """
    keep = frozenset(identity_keys)
    return {k: v for k, v in env_pairs.items() if k in keep or not is_secret_env_key(k)}


#: The one declared env key whose value the gateway's own writer builds from the
#: install's ``site-packages`` (``apps/bridges.py`` host-CLI pin). Only its
#: segments may fold that alias; every other variable hashes as written.
_SITE_PACKAGES_ENV_KEY = "PYTHONPATH"


def env_value_bytes(key: str, value: str) -> bytes:
    """The bytes :func:`hash_declared_env` folds in for one declared env value.

    A ``PYTHONPATH`` value is read as an ``os.pathsep`` list and each segment
    goes through :func:`launch_token_bytes` with the ``site-packages`` role, so
    a segment that IS the install's own ``site-packages`` -- the pin
    ``apps/bridges.py`` writes for the ``kirocrew`` host CLI -- hashes by alias
    like the interpreter beside it. A value with no alias segment re-joins to
    exactly its UTF-8 encoding (splitting on the separator and joining with the
    same separator is the identity). Any OTHER key hashes its value as written:
    ``PYTHONPATH`` is the only variable the gateway itself fills with that
    directory, so the same spelling under another name is operator- or
    agent-written content and stays a changed launch when it moves.
    """
    if key != _SITE_PACKAGES_ENV_KEY:
        return value.encode("utf-8")
    return os.pathsep.encode("utf-8").join(
        launch_token_bytes(segment, _ALIAS_SITE_PACKAGES) for segment in value.split(os.pathsep)
    )


def hash_declared_env(env_pairs: Mapping[str, str]) -> str:
    """Sorted ``K=V\\0``-delimited SHA-256 over every declared env pair.

    Values go through :func:`env_value_bytes` with their key: the launch
    approval fingerprint is this hash beside :func:`hash_command`, and both
    halves must fold the install's own paths or an upgrade still invalidates
    the approval through whichever half kept the versioned path. Only the
    ``PYTHONPATH`` value folds; the key decides, not the value.
    """
    h = hashlib.sha256()
    for k in sorted(env_pairs):
        h.update(k.encode("utf-8"))
        h.update(b"=")
        h.update(env_value_bytes(k, env_pairs[k]))
        h.update(b"\0")
    return h.hexdigest()


def hash_effective_env(env_pairs: Mapping[str, str], *, identity_keys: Collection[str] = ()) -> str:
    """Sorted ``K=V\\0``-delimited SHA-256 over the NON-SECRET env pairs.

    Feeds the ``effective_env_hash`` dimension of
    :class:`kiro_crew.mcp_gateway.pool.PoolKey`. Implemented on top of
    :func:`non_secret_env` so the hashed set and the forwardable set are the
    same set by construction — including for ``identity_keys``, which widens
    both together and can therefore never widen one without the other.

    WRITER AND READER MUST PASS THE SAME ``identity_keys``. The stub computes
    this hash for its Register frame; ``gatewayd._declared_env_pairs`` recomputes
    it at cold spawn and refuses to forward on a mismatch. That gate is what
    makes the stub's copy of the list untrusted data rather than authority: a
    stub that claims a different set than the daemon's configured one produces a
    hash the daemon does not reproduce, so forwarding fails closed.
    """
    filtered = non_secret_env(env_pairs, identity_keys=identity_keys)
    return hash_declared_env(filtered)


#: Fields :func:`format_pool_label` reads. Both producers hold all of them: a
#: Register payload by construction, and a
#: :class:`~kiro_crew.mcp_gateway.pool.PoolKey` as dataclass fields.
_LABEL_FIELDS = (
    "agent_name",
    "server_name",
    "os_uid",
    "command_args_hash",
    "effective_env_hash",
    "work_dir",
)


def format_pool_label(fields: Mapping[str, Any]) -> str:
    """Short log-friendly label for a pool identity. NOT collision-resistant.

    Reads a Register payload or a :class:`PoolKey`'s own fields, whichever the
    caller holds, and both produce the same string for the same identity --
    which is the point, because the stub and the daemon log the same session
    and an operator matches the two by eye.

    It lives in this leaf rather than on ``PoolKey`` so a caller that needs the
    label does not have to import ``pool``: that module's body resolves the
    read-buffer ceiling, which reads config, and ``mcp_gateway.stub`` wants the
    label for one log line per process. ``PoolKey.human_readable`` calls this,
    so there is one formatter and not two spellings to drift apart.

    Raises :class:`ValueError` naming every field it did not find.
    """
    missing = [name for name in _LABEL_FIELDS if name not in fields]
    if missing:
        raise ValueError(f"pool label fields missing: {missing}")

    def short(value: object) -> str:
        text = str(value)
        return (text[:8] + "\u2026") if len(text) > 8 else text

    return (
        f"{fields['agent_name']}:{fields['server_name']} "
        f"uid={fields['os_uid']} "
        f"cmd={short(fields['command_args_hash'])} "
        f"env={short(fields['effective_env_hash'])} ws={fields['work_dir']}"
    )
