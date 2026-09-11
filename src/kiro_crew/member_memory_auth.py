"""Gateway-owned process bindings and delegated proofs for private memory APIs.

The shared internal secret authenticates a local component, never a member.
Member authority follows kernel process ancestry into a sandbox-readonly record.
Absent ancestry is not host authority: inherited OS isolation must also agree.
Pooled MCP backends receive a process-bound proof from their trusted gateway.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, fsync_dir
from kiro_crew.config.paths import config_dir

PROOF_HEADER = "X-Member-Session-Proof"
PROOF_META_KEY = "memberMemoryProof"
_PROOF_TTL = 60
_MAX_RECORD_BYTES = 4096


def _binding_path(pid: int, home: Path) -> Path:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise ValueError("Invalid member process identity")
    path = home.resolve() / "member-memory-bindings" / "pids" / f"{pid}.json"
    if path.resolve() != path:
        raise ValueError("Member process identity path is redirected")
    return path


def _session_binding_path(session_key: str) -> Path:
    if not isinstance(session_key, str) or not session_key:
        raise ValueError("A private session key is required")
    digest = hashlib.sha256(session_key.encode()).hexdigest()
    path = config_dir().resolve() / "member-memory-bindings" / "sessions" / digest / "memory.json"
    if path.resolve() != path:
        raise ValueError("The private session binding path is redirected")
    return path


def _read_private_binding_file(path: Path, session_key: str) -> str | None:
    from kiro_crew.session_pid_sig import _read_regular_nofollow

    raw = _read_regular_nofollow(path)
    if raw is None:
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError(
                "The protected member session binding is missing or unreadable"
            ) from exc
        raise ValueError("The protected member session binding is missing or unreadable")
    try:
        row = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("The protected member session binding is invalid") from exc
    if (
        not isinstance(row, dict)
        or row.get("version") != 1
        or row.get("session_key") != session_key
    ):
        raise ValueError("The protected member session binding is invalid")
    store = row.get("memory_store")
    if not isinstance(store, str) or not store or store == "default":
        raise ValueError("The protected member session has no private store")
    return store


def read_private_session_store(session_key: str) -> str | None:
    """Read the immutable gateway binding; missing/corrupt existing records refuse."""
    path = _session_binding_path(session_key)
    store = _read_private_binding_file(path, session_key)
    if store is not None:
        return store
    if path.parent.exists():
        raise ValueError("The protected member session binding is missing or unreadable")
    return None


def private_history_session_index() -> dict[str, tuple[str, ...]]:
    """Map transcript spellings from protected records once per history request.

    Filename folding is lossy. Preserve all candidates so a collision cannot
    make one member's transcript look like another member's own history.
    This snapshot supplies names only; each selected record is revalidated.
    """
    from kiro_crew.history import transcript_stems
    from kiro_crew.session_pid_sig import _read_regular_nofollow

    root = config_dir().resolve() / "member-memory-bindings" / "sessions"
    if root.resolve() != root:
        raise ValueError("The private history binding directory is redirected")
    if not root.exists():
        return {}
    index: dict[str, list[str]] = {}
    for count, directory in enumerate(root.iterdir(), start=1):
        if count > 100_000:
            raise ValueError("Private history binding lookup exceeded its record limit")
        # Atomic publication leaves only digest directories committed. A
        # crash-left dot-prefixed staging sibling is not an identity record.
        if len(directory.name) != 64 or any(c not in "0123456789abcdef" for c in directory.name):
            continue
        if directory.resolve() != directory or not directory.is_dir():
            raise ValueError("The private history binding directory is invalid")
        path = directory / "memory.json"
        raw = _read_regular_nofollow(path)
        if raw is None:
            raise ValueError("A committed private history binding is unreadable")
        row = json.loads(raw)
        key = row.get("session_key") if isinstance(row, dict) else None
        if not isinstance(key, str) or _session_binding_path(key) != path:
            raise ValueError("A private history binding does not match its canonical key")
        store = row.get("memory_store")
        if row.get("version") != 1 or not isinstance(store, str) or not store or store == "default":
            raise ValueError("A committed private history binding is invalid")
        for spelling in dict.fromkeys((key, *transcript_stems(key))):
            index.setdefault(spelling, []).append(key)
    return {spelling: tuple(keys) for spelling, keys in index.items()}


def _same_private_binding(existing: str, memory_store: str) -> None:
    if existing != memory_store:
        raise ValueError("This session is already bound to another member; open a new conversation")


def require_memory_consolidation_session_key(session_key: str, expected_store: str) -> None:
    """Validate the exact generated key eligible for transient cleanup."""
    from kiro_crew.memory_stores import validate_memory_store_name

    validate_memory_store_name(expected_store)
    prefix = "memory-consolidation:"
    if not isinstance(session_key, str) or not session_key.startswith(prefix):
        raise ValueError("A generated memory consolidation session key is required")
    store, separator, nonce = session_key[len(prefix) :].rpartition(":")
    if separator != ":" or store != expected_store or re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise ValueError("The memory consolidation session key does not match its store")


def retire_memory_consolidation_binding(session_key: str, expected_store: str) -> bool:
    """Remove one generated binding after its provider has fully retired.

    The caller owns the lifecycle proof that no process can still use this
    authority. A missing committed file inside an existing digest directory is
    still corruption and is refused rather than treated as an idempotent cleanup.
    """
    require_memory_consolidation_session_key(session_key, expected_store)
    path = _session_binding_path(session_key)
    existing = _read_private_binding_file(path, session_key)
    if existing is None:
        if path.parent.exists():
            raise ValueError("The protected member session binding is missing or unreadable")
        return False
    _same_private_binding(existing, expected_store)
    try:
        entries = list(path.parent.iterdir())
    except OSError as exc:
        raise ValueError("The protected member session binding is unreadable") from exc
    if entries != [path]:
        raise ValueError("The protected member session binding directory has unexpected content")
    # Retire the committed directory in one rename. Readers ignore dot-prefixed
    # siblings, so a crash or cleanup error after this point can leave only an
    # inert residue, never a committed digest directory with memory.json gone.
    parent = path.parent.parent
    retired = parent / f".{path.parent.name}.retired-{secrets.token_hex(8)}"
    os.rename(path.parent, retired)
    fsync_dir(parent)
    (retired / path.name).unlink()
    retired.rmdir()
    fsync_dir(parent)
    return True


def _publish_private_binding_dir(staging: Path, destination: Path) -> None:
    """Atomically publish a complete sibling directory without replacement."""
    if platform_compat.IS_WINDOWS:
        # Windows rename refuses an existing directory destination.
        os.rename(staging, destination)
        return
    parent_fd = os.open(staging.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        platform_compat.rename_noreplace(
            staging.name,
            destination.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    finally:
        os.close(parent_fd)


def bind_private_session_store(session_key: str, memory_store: str) -> None:
    """First trusted private preparation pins this session permanently to its member."""
    from kiro_crew.memory_stores import memory_store_version, require_memory_store

    require_memory_store(memory_store)
    if memory_store_version(memory_store) != 2:
        raise ValueError("Only private V2 memory can bind a private session")
    path = _session_binding_path(session_key)
    for directory in (path.parent.parent.parent, path.parent.parent):
        platform_compat.make_owner_only_dir(directory)
        platform_compat.restrict_dir_to_owner(directory)

    existing = _read_private_binding_file(path, session_key)
    if existing is not None:
        _same_private_binding(existing, memory_store)
        return
    if path.parent.exists():
        # The directory itself is the committed marker. Never repair a missing
        # or unreadable record by adopting a caller's new claim.
        existing = _read_private_binding_file(path, session_key)
        if existing is None:
            raise ValueError("The protected member session binding is missing or unreadable")
        _same_private_binding(existing, memory_store)
        return

    staging = Path(
        tempfile.mkdtemp(prefix=f".{path.parent.name}.", suffix=".tmp", dir=path.parent.parent)
    )
    try:
        platform_compat.restrict_dir_to_owner(staging)
        atomic_write(
            staging / path.name,
            json.dumps({"version": 1, "session_key": session_key, "memory_store": memory_store}),
            fsync=True,
            newline="",
            restrict_to_owner=True,
        )
        fsync_dir(staging)
        try:
            _publish_private_binding_dir(staging, path.parent)
        except FileExistsError:
            existing = _read_private_binding_file(path, session_key)
            if existing is None:
                raise ValueError("The protected member session binding is missing or unreadable")
            _same_private_binding(existing, memory_store)
        else:
            fsync_dir(path.parent.parent)
    finally:
        # A crash-left staging sibling is inert because readers recognize only
        # the final digest directory. Ordinary failures clean their own stage.
        shutil.rmtree(staging, ignore_errors=True)


def publish_member_session_pid(
    pid: int, session_key: str, *, home: Path | None = None, memory_store: str | None = None
) -> None:
    """Trusted publisher only; an unknown process incarnation grants nothing."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return
    path = _binding_path(pid, home or config_dir())
    store = private_memory_store_for_session(session_key) if memory_store is None else memory_store
    start = platform_compat.get_process_start_id(pid)
    if not start or not isinstance(session_key, str) or not session_key:
        path.unlink(missing_ok=True)
        return
    for directory in (path.parent.parent, path.parent):
        platform_compat.make_owner_only_dir(directory)
        platform_compat.restrict_dir_to_owner(directory)
    atomic_write(
        path,
        json.dumps(
            {
                "version": 2,
                "session_key": session_key,
                "process_start": start,
                "memory_store": store,
            }
        ),
    )


def _private_memory_mcp_failure(backend: str) -> str:
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_CODEX, ACP_BACKENDS_PRIVATE_MEMORY_MCP

    if backend not in ACP_BACKENDS_PRIVATE_MEMORY_MCP:
        label = {ACP_BACKEND_CODEX: "Codex ACP"}.get(backend, "The selected ACP backend")
        return (
            f"{label} cannot run private member MCP tools directly. Use Kiro, Claude Code or KAS: "
            "set the member backend for private chat, and the default backend for Crew tasks, "
            "schedules and memory consolidation."
        )
    return ""


def require_private_memory_mcp_backend(backend: str) -> None:
    """Validate the actual private runtime backend before constructing it."""
    reason = _private_memory_mcp_failure(backend)
    if reason:
        from kiro_crew.memory_stores import UnknownMemoryStore

        raise UnknownMemoryStore(reason + " Global Memory V1 was not used.")


def private_memory_execution_supported(*, session_key: str = "") -> bool:
    """Use the spawn layer's mode/floor/backend decisions, excluding delegation."""
    return not _private_memory_execution_failure(session_key=session_key)


def _private_memory_execution_failure(*, session_key: str = "") -> str:
    if sys.platform == "win32":
        return "Native Windows cannot enforce private member filesystem isolation. Use the WSL/Linux gateway."
    from kiro_crew import sandbox
    from kiro_crew.acp_backends import ACP_BACKENDS_INTERNAL_SANDBOX
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.members import select_provider_backend

    try:
        config = KiroCrewConfig.load()
        backend = select_provider_backend(
            session_key, config.agent.member_acp_backend, config.agent.acp_backend
        )
        mcp_failure = _private_memory_mcp_failure(backend)
        if mcp_failure:
            return mcp_failure
        mode = sandbox._clamp_sandbox_mode(config.agent.sandbox)
        if mode == "off":
            return "Crew's OS sandbox is off. Enable agent.sandbox and restart the gateway before running private members."
        if sys.platform == "darwin":
            if backend in ACP_BACKENDS_INTERNAL_SANDBOX and sandbox.kiro_internal_sandbox_enabled():
                return "Kiro internal sandbox delegation bypasses Crew's private member filesystem fences. Disable that delegation and restart with Crew's outer Seatbelt sandbox."
        if sandbox.detect_backend(config_mode=mode) not in {"namespace", "sandbox-exec"}:
            mechanism = (
                "outer Seatbelt sandbox"
                if sys.platform == "darwin"
                else "Linux user/mount namespaces"
            )
            return f"Crew could not activate {mechanism}. Restore OS sandbox support before running private members."
        return ""
    except Exception as exc:
        return f"Private member sandbox configuration could not be verified ({type(exc).__name__}). Check the gateway's sandbox configuration and restart."


def require_private_memory_execution(*, session_key: str = "") -> None:
    """Fail before provider startup when private files cannot be OS-isolated."""
    if not private_memory_execution_supported(session_key=session_key):
        from kiro_crew.memory_stores import UnknownMemoryStore

        reason = _private_memory_execution_failure(session_key=session_key)
        raise UnknownMemoryStore(
            (reason or "Private member OS filesystem isolation could not be verified.")
            + " Member management remains available; Global Memory V1 was not used."
        )


def require_member_memory_creation(member: str) -> None:
    """Admit new private allocation under the operator setting and execution route."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG
    from kiro_crew.members import member_thread_session_alias, slug_for_name
    from kiro_crew.memory_stores import UnknownMemoryStore

    cfg = KiroCrewConfig.load()
    if cfg.degraded_sections & {"memory", DEGRADED_WHOLE_CONFIG}:
        raise UnknownMemoryStore(
            "Private memory creation requires readable memory configuration. "
            "Repair config.json before creating new members or choosing V1-to-V2 setup."
        )
    if not cfg.memory.private_provisioning_enabled:
        raise UnknownMemoryStore(
            "New private memory creation is paused by memory.private_provisioning_enabled. "
            "Set it to true to allow new members or V1-to-V2 setup. Existing memory remains available."
        )
    require_private_memory_execution(session_key=member_thread_session_alias(slug_for_name(member)))


def verified_member_session_for_pid(peer_pid: int, *, home: Path | None = None) -> str:
    """Resolve the first recorded real ancestor; corrupt or recycled records refuse."""
    return protected_member_session_for_pid(peer_pid, home=home) or ""


def protected_member_session_for_pid(peer_pid: int, *, home: Path | None = None) -> str | None:
    """None means no private binding, not host proof; empty means invalid identity."""
    binding = _protected_member_binding_for_pid(peer_pid, home=home)
    return None if binding is None or (binding[0] and not binding[1]) else binding[0]


def _protected_member_binding_for_pid(
    peer_pid: int, *, home: Path | None = None
) -> tuple[str, str] | None:
    from kiro_crew.session_pid_sig import _read_regular_nofollow

    base = home or config_dir()
    pid = peer_pid
    seen: set[int] = set()
    for _ in range(128):
        if not isinstance(pid, int) or pid <= 1 or pid in seen:
            return None
        seen.add(pid)
        try:
            path = _binding_path(pid, base)
            if path.exists():
                raw = _read_regular_nofollow(path)
                if raw is None:
                    return "", ""
                row = json.loads(raw)
                if not isinstance(row, dict) or row.get("version") not in (1, 2):
                    return "", ""
                start = platform_compat.get_process_start_id(pid)
                recorded_start = row.get("process_start")
                if (
                    start
                    and isinstance(recorded_start, str)
                    and recorded_start
                    and start != recorded_start
                ):
                    # This record describes a dead incarnation, not this live
                    # process. Ignore it without granting authority; a real
                    # ancestor or positive host provenance must still identify it.
                    pid = platform_compat.get_ppid(pid)
                    continue
                key = row.get("session_key")
                store = row.get("memory_store")
                if (
                    not start
                    or start != row.get("process_start")
                    or not isinstance(key, str)
                    or not key
                    or not isinstance(store, str)
                    or store == "default"
                    or (not store and row.get("version") != 2)
                ):
                    return "", ""
                if not store and sys.platform == "linux":
                    # A V1 runtime is positively published too, but a nested
                    # private namespace cannot inherit that runtime's authority.
                    if platform_compat.process_namespaces_match(
                        peer_pid, pid
                    ) is not True and not _matches_published_sandbox_namespace(
                        peer_pid, pid, start, base
                    ):
                        return "", ""
                if not store and sys.platform == "darwin":
                    # Both V1 and private runtimes use Seatbelt. Check the
                    # caller's actual Global permission, so a private descendant
                    # cannot borrow an empty-store ancestor after losing its
                    # own binding, while ordinary sandboxed V1 keeps working.
                    if (
                        platform_compat.process_can_read_under_sandbox(
                            peer_pid, base.resolve() / "memory.db"
                        )
                        is not True
                    ):
                        return "", ""
                return key, store
            pid = platform_compat.get_ppid(pid)
        except (OSError, ValueError, RuntimeError):
            return "", ""
    return "", ""


def _matches_published_sandbox_namespace(
    peer_pid: int, launcher_pid: int, launcher_start: str, home: Path
) -> bool:
    """Only the trusted launcher parent can publish its child's namespace pair."""
    from kiro_crew.session_pid_sig import _read_regular_nofollow

    path = _binding_path(launcher_pid, home).with_suffix(".namespace.json")
    try:
        raw = _read_regular_nofollow(path)
        if raw is None:
            return False
        row = json.loads(raw)
        if (
            not isinstance(row, dict)
            or row.get("process_start") != launcher_start
            or row.get("private_memory") is not False
        ):
            return False
        peer_start = platform_compat.get_process_start_id(peer_pid)
        if not peer_start:
            return False
        namespaces = []
        for kind in ("user", "mnt"):
            info = Path(f"/proc/{peer_pid}/ns/{kind}").stat()
            namespaces.append([info.st_dev, info.st_ino])
        return (
            namespaces == row.get("namespaces")
            and platform_compat.get_process_start_id(peer_pid) == peer_start
            and platform_compat.get_process_start_id(launcher_pid) == launcher_start
        )
    except (OSError, ValueError, TypeError):
        return False


def _verified_host_process(pid: int) -> bool:
    """Positive host provenance survives loss of every recorded ancestor.

    Linux private descendants retain a different user/mount namespace, even
    after reparenting or a gateway restart. Seatbelt is inherited on macOS. An
    unknown sandboxed caller therefore fails closed instead of becoming V1 or
    owner. Native Windows has no private runtime: its spawn gate refuses one.
    """
    if sys.platform == "linux":
        return platform_compat.process_namespaces_match(pid, os.getpid()) is True
    if sys.platform == "darwin":
        return platform_compat.process_is_sandboxed(pid) is False
    return sys.platform == "win32"


def _verified_global_process(pid: int) -> bool:
    binding = _protected_member_binding_for_pid(pid)
    if binding is not None:
        return bool(binding[0] and not binding[1])
    return _verified_host_process(pid)


def _proof_key(*, create: bool) -> bytes | None:
    path = config_dir().resolve() / "memory_stores" / ".member-api-key"
    if path.resolve() != path:
        return None
    if create:
        platform_compat.make_owner_only_dir(path.parent)
        platform_compat.restrict_dir_to_owner(path.parent)
        try:
            with path.open("rb") as handle:
                existing = handle.read(33)
        except FileNotFoundError:
            existing = None
        except OSError:
            return None
        if existing is not None:
            # A committed short key is corruption, not permission to rotate the
            # signing identity. Keep refusing until the operator repairs it.
            return existing if len(existing) == 32 else None

        # Stage the whole key under the protected memory root. ``atomic_write``
        # applies the owner-only ACL before writing any secret bytes and fsyncs
        # the completed file. The hard-link publish is atomic and no-replace on
        # POSIX and Windows: concurrent first creators converge on one inode,
        # while a crash before the link leaves the final name absent rather than
        # permanently committing an empty key.
        staged = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
        try:
            atomic_write(
                staged,
                secrets.token_bytes(32),
                fsync=True,
                restrict_to_owner=True,
            )
            try:
                os.link(staged, path)
            except FileExistsError:
                # Another process published a complete key first. Read that
                # winner below; never replace it with this process's candidate.
                pass
            else:
                # The staged inode was durable before publication. Sync the new
                # final directory entry before dropping its temporary hard link.
                fsync_dir(path.parent)
        finally:
            staged.unlink(missing_ok=True)
    try:
        with path.open("rb") as handle:
            key = handle.read(33)
        return key if len(key) == 32 else None
    except OSError:
        return None


def _proof_process_scope(pid: int) -> list[Any] | None:
    """Snapshot the live isolation identity; an unreadable identity refuses."""
    try:
        if sys.platform == "linux":
            identity: list[Any] = ["linux"]
            for namespace in ("user", "mnt"):
                info = Path(f"/proc/{pid}/ns/{namespace}").stat()
                identity.append([info.st_dev, info.st_ino])
            return identity
        if sys.platform == "darwin":
            sandboxed = platform_compat.process_is_sandboxed(pid)
            return ["darwin", sandboxed] if isinstance(sandboxed, bool) else None
        # Native Windows has no private execution path. Keeping the process
        # identity representation permits trusted management and protocol tests;
        # it does not relax require_private_memory_execution's Windows refusal.
        if sys.platform == "win32":
            return ["win32"]
    except (OSError, ValueError):
        return None
    return None


def _proof_payload(proof: str) -> dict[str, Any] | None:
    """Decode a proof's claims without checking its signature; None when malformed."""
    payload = proof.split(".")[0]
    row = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return row if isinstance(row, dict) else None


def issue_member_session_proof(session_key: str, peer_pid: int) -> str:
    """Delegate only the originating process's current protected member store."""
    binding = _protected_member_binding_for_pid(peer_pid)
    if not binding or binding[0] != session_key or not binding[1]:
        return ""
    start = platform_compat.get_process_start_id(peer_pid)
    scope = _proof_process_scope(peer_pid)
    key = _proof_key(create=True)
    if (
        not session_key
        or not start
        or scope is None
        or key is None
        or read_private_session_store(session_key) != binding[1]
        or platform_compat.get_process_start_id(peer_pid) != start
    ):
        return ""
    body = json.dumps(
        {"v": 2, "s": session_key, "p": peer_pid, "i": start, "m": binding[1], "n": scope},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    payload = base64.urlsafe_b64encode(body).decode().rstrip("=")
    digest = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{digest}"


def verify_member_session_proof(proof: str, session_key: str) -> bool:
    """A proof remains valid only while its live process retains this session."""
    if not isinstance(proof, str) or len(proof) > _MAX_RECORD_BYTES:
        return False
    try:
        payload, supplied = proof.split(".")
        key = _proof_key(create=False)
        if key is None or not hmac.compare_digest(
            supplied, hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
        ):
            return False
        row = _proof_payload(proof)
        if (
            row is None
            or type(row.get("v")) is not int
            or row["v"] not in (1, 2)
            or row.get("s") != session_key
        ):
            return False
        if row["v"] == 1:
            now = time.time()
            if type(row.get("e")) is not int or not now < row["e"] <= now + _PROOF_TTL:
                return False
        pid = row.get("p")
        if not (
            isinstance(pid, int)
            and not isinstance(pid, bool)
            and isinstance(row.get("i"), str)
            and row["i"]
            and platform_compat.get_process_start_id(pid) == row["i"]
        ):
            return False
        if row["v"] == 1:
            return verified_member_session_for_pid(pid) == session_key
        # Protocol v2 lasts for this process incarnation, not an arbitrary wall
        # clock interval. Every use rechecks its store, session and OS isolation.
        # A long-running tool may retain its invocation proof; another process
        # incarnation, changed binding or isolation view cannot inherit it.
        store = row.get("m")
        scope = _proof_process_scope(pid)
        return bool(
            isinstance(store, str)
            and store
            and store != "default"
            and _protected_member_binding_for_pid(pid) == (session_key, store)
            and read_private_session_store(session_key) == store
            and scope is not None
            and scope == row.get("n")
            and platform_compat.get_process_start_id(pid) == row["i"]
        )
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


def private_memory_request_verified(request: Any) -> bool:
    """Unverifiable callers always fail closed, including transport/read failures."""
    try:
        return _private_memory_request_verified(request)
    except Exception:
        return False


def _private_memory_request_verified(request: Any) -> bool:
    """Positive member proof, independent of the caller-supplied session header."""
    key = request.headers.get("X-Session-Key", "")
    actual, verified = memory_request_identity(request)
    # HTTP headers are strings; identity returns str|None and bool. This cast
    # receives an empty string or a boolean, never a parsed number or NaN.
    # nosemgrep: python.django.security.nan-injection.nan-injection
    return bool(key and verified and actual == key)


def memory_request_identity(request: Any) -> tuple[str | None, bool]:
    """Authenticate the caller before interpreting any requested store or header.

    A verified None is an unowned process. An unverifiable identity grants no
    downgrade authority, even when the requested destination happens to be V1.
    """
    try:
        if request.get("internal_auth") is not True:
            return None, False
        proof = request.headers.get(PROOF_HEADER, "")
        if proof:
            if not isinstance(proof, str) or len(proof) > _MAX_RECORD_BYTES:
                return None, False
            row = _proof_payload(proof)
            key = row.get("s") if row is not None else None
            if not isinstance(key, str) or not key or not verify_member_session_proof(proof, key):
                return None, False
            return key, True
        pid = _request_peer_pid(request)
        if not isinstance(pid, int) or not platform_compat.get_process_start_id(pid):
            return None, False
        # One ancestry walk answers all three outcomes: a private binding, a
        # positively published Global runtime (empty store), or no record at
        # all, which falls back to host provenance.
        binding = _protected_member_binding_for_pid(pid)
        if binding is None:
            return None, _verified_host_process(pid)
        session, store = binding
        if session and not store:
            return None, True
        return session, session != ""
    except Exception:
        return None, False


def memory_request_bound_store(request: Any) -> str | None:
    """After identity verification, refuse any rekey before target resolution."""
    try:
        proof = request.headers.get(PROOF_HEADER, "")
        row: dict[str, Any] = {}
        if proof:
            decoded = _proof_payload(proof)
            if decoded is None:
                return None
            row = decoded
            pid = row.get("p")
            session_key = row.get("s")
        else:
            pid = _request_peer_pid(request)
            session_key = request.headers.get("X-Session-Key", "")
        if not isinstance(pid, int) or isinstance(pid, bool):
            return None
        binding = _protected_member_binding_for_pid(pid)
        if not binding or not binding[0] or binding[0] != session_key:
            return None
        if proof and row.get("v") == 2 and binding[1] != row.get("m"):
            return None
        return binding[1]
    except (OSError, ValueError, TypeError, RuntimeError):
        return None


def mcp_memory_scope(session_key: str) -> str:
    """Verify MCP authority before a host tool uses a private session identity."""
    from kiro_crew.mcp_caller import current_caller

    caller = current_caller()
    proof = caller.member_memory_proof if caller is not None else ""
    process_session = protected_member_session_for_pid(os.getpid())
    if process_session is not None and (not process_session or process_session != session_key):
        raise ValueError("The MCP session does not match its protected process")
    if proof and not verify_member_session_proof(proof, session_key):
        raise ValueError("The MCP member proof is invalid")
    store = private_memory_store_for_session(session_key)
    if store and not proof and process_session != session_key:
        raise ValueError("Private MCP access requires a protected process or member proof")
    return store


def private_memory_store_for_session(session_key: str | None) -> str:
    """Trusted gateway-only resolution used before process allocation/publication."""
    if not session_key or not private_memory_boundaries_active():
        return ""
    from kiro_crew.context import store_of_session
    from kiro_crew.history import ConversationLog
    from kiro_crew.memory_stores import memory_store_version

    store = store_of_session(ConversationLog(), session_key)
    return store if store and memory_store_version(store) == 2 else ""


def private_memory_boundaries_active() -> bool:
    """Pure V1 installations retain their existing internal API contract."""
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        if (config_dir() / "member-memory-bindings" / "sessions").exists():
            return True
        return any(
            store.memory_version == 2 for store in KiroCrewConfig.load().memory_stores.values()
        )
    except Exception:
        return True


def local_owner_bootstrap_allowed(request: Any) -> bool:
    """The shared local secret cannot promote a private member into the owner."""
    try:
        if not private_memory_boundaries_active():
            return True
        pid = _request_peer_pid(request)
        return bool(
            isinstance(pid, int)
            and platform_compat.get_process_start_id(pid)
            and protected_member_session_for_pid(pid) is None
            and _verified_host_process(pid)
        )
    except Exception:
        return False


def _request_peer_pid(request: Any) -> int | None:
    from kiro_crew.dashboard.token_auth import _unix_request_socket
    from kiro_crew.mcp_gateway.socketsec import PeerCredResult, check_peer_is_self, get_peer_pid

    sock = _unix_request_socket(request)
    if sock is not None:
        if check_peer_is_self(sock) is not PeerCredResult.MATCH:
            return None
        pid = get_peer_pid(sock)
    else:
        from kiro_crew.dashboard.origin import is_loopback

        transport = getattr(request, "transport", None)
        if transport is None:
            return None
        server = transport.get_extra_info("sockname")
        client = transport.get_extra_info("peername")
        if not (
            isinstance(server, tuple)
            and isinstance(client, tuple)
            and len(server) >= 2
            and len(client) >= 2
            and is_loopback(server[0])
            and is_loopback(client[0])
        ):
            return None
        resolve_tcp = getattr(platform_compat, "get_tcp_peer_pid", None)
        if resolve_tcp is None:
            return None
        pid = resolve_tcp(server[:2], client[:2])
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None
