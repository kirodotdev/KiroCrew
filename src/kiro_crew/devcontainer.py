"""Dev Container support: run a session's kiro-cli inside the project's
devcontainer (VS Code parity).

When ``agent.devcontainer`` is ``"auto"`` and a session's work dir carries a
``.devcontainer/devcontainer.json`` (or ``.devcontainer.json``), the ACP spawn
path replaces the host kiro-cli argv with a ``docker exec`` into a container
built by the reference ``@devcontainers/cli`` — the same engine VS Code uses.
The repo's devcontainer.json is honored for image/build, in-container
lifecycle hooks, mounts and runArgs after a one-time per-config human trust
grant, mirroring VS Code's Workspace Trust model. This is parity, not a
sandbox: the grant is the ceiling for what the hashed tree declares.

The screen is closed by class, not by docker spelling. See the class table
in ``docs/system-specs/modules/devcontainers.md`` (Closed screen).
``features`` and privilege surfaces (``privileged``, host namespaces,
mount-capable capabilities) are refused because they void that grant: a
Feature's metadata is fetched and merged at build time, so it is not the
text the grant bound, and a privileged container makes every other check
unenforceable. A new alias of an already-covered class is canonicalized
into that class. A ``runArgs`` flag that is not in a class is the grant.

Architecture (mirrors VS Code's client/server split):
  - gateway stays on the host (UI plane);
  - kiro-cli is executed INSIDE the container (execution plane), like
    vscode-server. Verified necessary: kiro-cli 2.14 executes shell/file
    tools in-process and ignores the ACP client fs/terminal capabilities,
    so the process itself must move.
  - the workspace is bind-mounted by the devcontainer CLI; the ACP
    ``session/new`` cwd uses the container-side workspace folder.

Trust model: the SHA-256 of the effective devcontainer.json must be granted
by a dashboard user before any build or exec. Config edits invalidate trust
(hash mismatch → re-prompt), matching VS Code's re-prompt on change.

Container reuse: one container per project directory, keyed by an id-label,
reused across sessions and gateway restarts (``devcontainer up`` is
idempotent for an unchanged config).

Managed MCP (cron, subagents, lessons) stays on the host. kiro-cli inside
the container talks to a stdio-to-unix-socket client bind-mounted at
``/tmp/kirocrew-mcp-bridge``; the host accept loop spawns ``kirocrew
mcp-core`` / ``mcp-cron`` / ``mcp-computer`` with the session's env so
their REST callbacks still hit gateway loopback. The image needs
``python3``. Pooling stubs are not injected on this path: they dial a
host-only socket and import ``kiro_crew``.

Known v1 limitations (documented in docs/devcontainers.md):
  - /proc-based liveness observes the host-side ``docker exec`` client
    proxy: death detection works (pipe close), wedge heuristics degrade.
  - Docker Desktop (macOS / Windows) is a VM: managed MCP uses TCP to
    ``host.docker.internal`` rather than a bind-mounted unix socket. The
    host Seatbelt / Job-object path is skipped on a containerized spawn,
    same as on native Linux.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir, inject_kiro_cli_api_key
from kiro_crew.constants import DEVCONTAINER_ENV_VAR as _DEVCONTAINER_ENV_VAR
from kiro_crew.constants import ENV_TRUTHY, KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE

try:  # optional dependency: compose screening needs a YAML parser
    import yaml as _yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - exercised by the refusal path
    _yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# jsonc comments are legal in devcontainer.json; strip for hashing/preview
# only — the devcontainer CLI does its own real parse.
_LINE_COMMENT_RE = re.compile(r"^\s*//.*$", re.MULTILINE)

# Marker env for processes exec'd into a container, so in-container helpers
# can identify their exec instance (kill file naming, diagnostics).
DEVCONTAINER_EXEC_ENV = "KIROCREW_DEVCONTAINER_EXEC"

# Where exec pid files live inside the container. tmpfs on most images.
_EXEC_PIDFILE_DIR = "/tmp/kirocrew-exec"

_UP_TIMEOUT_SECS = 15 * 60  # image build + feature install can be slow
_EXEC_PROBE_TIMEOUT_SECS = 20

#: Bytes of each build stream kept in memory. Only the TAIL is ever used -- the
#: CLI's outcome JSON is its last line, and the failure path shows a stderr tail
#: -- so a tail buffer costs a fixed amount however verbose the build is.
_UP_STREAM_KEEP_BYTES = 64 * 1024

#: Hard per-stream ceiling. Past this the build is treated as runaway and the
#: child is killed. Set far above any plausible build log, so a verbose but
#: legitimate lifecycle command is drained rather than refused; the tail buffer
#: above is what actually bounds memory.
_UP_STREAM_LIMIT_BYTES = 64 * 1024 * 1024


async def _read_capped(
    stream: asyncio.StreamReader | None,
    keep: int = _UP_STREAM_KEEP_BYTES,
    limit: int = _UP_STREAM_LIMIT_BYTES,
) -> tuple[bytes, bool]:
    """Drain *stream*, retaining only its last *keep* bytes.

    Returns the retained tail and whether the hard ceiling was reached.

    The stream is still read to the end (or to the ceiling) rather than left
    alone: an unread pipe fills its OS buffer and blocks the child, which would
    trade an out-of-memory failure for a silent hang.
    """
    if stream is None:
        return b"", False
    buf = bytearray()
    total = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(buf), False
        total += len(chunk)
        if total > limit:
            return bytes(buf), True
        buf += chunk
        if len(buf) > keep:
            del buf[:-keep]


class DevcontainerError(RuntimeError):
    """A devcontainer operation failed. Message is operator-facing."""


class DevcontainerNotTrusted(DevcontainerError):
    """The project's devcontainer.json has no valid trust grant."""


class DevcontainerConfigChanged(DevcontainerError):
    """The config changed between being shown to a human and being trusted.

    Distinct from DevcontainerNotTrusted so the dashboard can tell "you never
    approved this" from "what you approved is no longer what is on disk" and
    re-prompt with the new bytes rather than reporting a plain refusal.
    """


def find_devcontainer_config(project_dir: str | Path) -> Path | None:
    """Locate the project's devcontainer config, spec lookup order.

    ``.devcontainer/devcontainer.json`` wins over ``.devcontainer.json``.
    Returns None when the project has no devcontainer config.

    Symlink leaves are treated as absent: the config is read back to the
    caller and hashed for trust, so a link pointing outside the project
    (``.devcontainer/devcontainer.json -> ~/.aws/credentials``) would turn
    the preview endpoint into an arbitrary-file read. _read_config_bytes
    enforces the same property at open time (lstat here is advisory).
    """
    root = Path(project_dir)
    for candidate in (
        root / ".devcontainer" / "devcontainer.json",
        root / ".devcontainer.json",
    ):
        try:
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        except OSError:
            continue
    return None


def _project_root_of(config_path: Path) -> Path:
    """The project directory a config path belongs to (both spec layouts)."""
    parent = config_path.parent
    return parent.parent if parent.name == ".devcontainer" else parent


def _assert_fd_still_contained(fd: int, root: str, original: str) -> None:
    """Re-screen the OPENED descriptor for containment and sensitivity.

    The screens above ran on a path string that ``os.open`` then resolved a second
    time. ``O_NOFOLLOW`` covers only the final component, so an intermediate
    directory swapped between the two resolutions yields a descriptor pointing
    somewhere the screens never saw -- and the bytes read here reach the trust
    preview and the image build.

    On Linux ``/proc/self/fd/<n>`` names the inode the descriptor actually holds,
    which is the authoritative answer and the reason this is checked here rather
    than by looking at the path again.

    Where that is unavailable the check degrades to comparing the descriptor's
    identity against the validated path's, which catches a swap that is still in
    place but not one reverted in between. That is acceptable only because the
    resolver refuses every non-Linux platform long before this runs; the fallback
    exists so the helper is not silently unprotected if it ever gains a caller.
    """
    from kiro_crew.security import is_sensitive_path  # circular import

    actual: str | None = None
    try:
        actual = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        actual = None

    if actual is None:
        # No /proc: fall back to identity. A mismatch means the name we validated
        # and the inode we hold are not the same file.
        try:
            by_path = os.stat(original)
            by_fd = os.fstat(fd)
        except OSError as exc:
            raise DevcontainerError(
                f"cannot re-verify devcontainer config after opening it: {exc}"
            ) from exc
        if (by_path.st_dev, by_path.st_ino) != (by_fd.st_dev, by_fd.st_ino):
            raise DevcontainerError(
                f"devcontainer config {original} changed identity while it was "
                f"being opened; refusing to read a file the path screens did not "
                f"approve"
            )
        return

    # A deleted-then-replaced file reads as "<path> (deleted)"; treat anything
    # that is not a plain resolvable path as a refusal rather than parsing it.
    # os.path.isabs, not a leading-separator test: "C:\\..." is absolute and has no
    # leading separator, so a separator check silently reports every Windows path
    # as replaced -- reaching the right refusal by the wrong reasoning, and never
    # running the containment and sensitivity screens below.
    if actual.endswith(" (deleted)") or not os.path.isabs(actual):
        raise DevcontainerError(
            f"devcontainer config {original} was replaced while it was being "
            f"opened; refusing to read it"
        )
    if not actual.startswith(root.rstrip(os.sep) + os.sep):
        raise DevcontainerError(
            f"devcontainer config resolved outside the project once opened "
            f"({original}); an intermediate directory changed under the check"
        )
    if is_sensitive_path(actual):
        raise DevcontainerError(
            f"devcontainer config resolved to a sensitive path once opened "
            f"({original}); an intermediate directory changed under the check"
        )


def _read_config_bytes(config_path: Path, root_dir: Path | None = None) -> bytes:
    """Read a devcontainer input refusing symlinks, escapes, and sensitive targets.

    Defense in depth for the trust-preview read path (the bytes go back to
    the dashboard caller verbatim):
      1. O_NOFOLLOW on the final component — a symlink leaf fails with ELOOP
         even if it appeared between lookup and open (TOCTOU);
      2. fstat must report a regular file;
      3. the realpath must stay inside the project root — covers a symlinked
         PARENT directory (.devcontainer -> elsewhere), which O_NOFOLLOW on
         the leaf cannot see;
      4. is_sensitive_path screen on the resolved target.

    ``root_dir`` names the directory the target must stay inside. Tree members
    pass the project root explicitly: inferring it from a nested path would
    yield that file's own parent, making the containment check in (3) a
    tautology that any nested file trivially satisfies.
    """
    from kiro_crew.security import is_sensitive_path  # circular import

    resolved = os.path.realpath(config_path)
    root = os.path.realpath(root_dir or _project_root_of(config_path))
    if not resolved.startswith(root.rstrip(os.sep) + os.sep):
        raise DevcontainerError(f"devcontainer config resolves outside the project: {config_path}")
    if is_sensitive_path(resolved):
        raise DevcontainerError(f"devcontainer config resolves to a sensitive path: {config_path}")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    # O_NONBLOCK so the fstat check below is actually REACHED. Opening a FIFO
    # without it blocks until a writer appears, and since this runs under
    # asyncio.to_thread on every dashboard status poll, one FIFO planted in
    # .devcontainer/ would wedge a worker per poll and starve the shared
    # executor. Harmless for regular files, which is all this accepts anyway.
    nonblock = getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(str(config_path), os.O_RDONLY | nofollow | nonblock)
    except OSError as exc:
        raise DevcontainerError(f"cannot open devcontainer config: {exc}") from exc
    try:
        # Before ANY read: the screens above approved a path, and the open
        # resolved that path again. Re-screen what the descriptor actually holds.
        _assert_fd_still_contained(fd, root, str(config_path))
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise DevcontainerError(f"devcontainer config is not a regular file: {config_path}")
        # A HARD LINK is indistinguishable from an ordinary file by path: the
        # symlink refusal and the sensitive-path screen both see a benign name
        # inside .devcontainer/ while the inode is the credential file itself, and
        # a Dockerfile COPY then bakes it into an agent-readable image. Link count
        # is the only local signal, so more than one name for this inode is
        # refused. Checked off the SAME fstat as the mode, since a separate stat
        # would be a second look at a path that can change underneath.
        if st.st_nlink != 1:
            raise DevcontainerError(
                f"devcontainer input {config_path} has {st.st_nlink} hard links; a "
                f"second name for the same inode can point at a file outside the "
                f"config tree, which the path screens cannot see"
            )
        # Size is checked HERE, off the same fstat as the mode and link count,
        # because the caller's pre-open stat() is a different file: between that
        # stat and this open the path can be replaced, so a member that measured
        # small can be read as an arbitrarily large one and exhaust gateway
        # memory. This walk is reachable from dashboard status polling, so the
        # bound has to hold against the inode actually opened.
        if st.st_size > _MAX_TREE_FILE_BYTES:
            raise DevcontainerError(
                f"devcontainer input {config_path.name} is {st.st_size} bytes, over "
                f"the {_MAX_TREE_FILE_BYTES}-byte per-file limit for a hashed tree"
            )
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            # Bounded read: st_size is a snapshot, and a writer appending after
            # the fstat would otherwise let read() return more than the ceiling.
            # One extra byte is requested so growth past the limit is detected
            # rather than silently truncated into a digest.
            data = fh.read(_MAX_TREE_FILE_BYTES + 1)
            if len(data) > _MAX_TREE_FILE_BYTES:
                raise DevcontainerError(
                    f"devcontainer input {config_path.name} grew past the "
                    f"{_MAX_TREE_FILE_BYTES}-byte per-file limit while being read"
                )
            return data
    finally:
        if fd >= 0:
            os.close(fd)


def _read_config_tree(config_path: Path) -> list[tuple[str, bytes]]:
    """Read the whole devcontainer input set ONCE into memory.

    Returns ``[(relpath, bytes), ...]`` sorted by relpath, with the config
    itself present under its own relative name. This single pass is what makes
    the digest and the preview text describe the SAME bytes: computing them
    from two separate walks let an agent swap the tree in between, so the card
    could display benign text bound to a different tree's digest.

    A symlink ANYWHERE in the tree is refused rather than skipped. Skipping one
    would leave it outside the digest, so its target could be retargeted (or its
    content swapped) after the grant without changing the hash, and a lifecycle
    hook like ``bash setup.sh`` would then run unreviewed code under a
    still-valid trust. Refusing fails closed instead.

    Blocking I/O. Callers on the event loop must offload it.
    """
    parent = config_path.parent
    if parent.name != ".devcontainer":
        # Root-layout ``.devcontainer.json``: one file, no directory.
        return [(config_path.name, _read_config_bytes(config_path))]

    # ``rglob`` never yields the parent itself, so the per-entry symlink check
    # below cannot see a symlinked ``.devcontainer`` dir. Refuse it here: every
    # member would resolve outside the project, and the preview returns these
    # bytes verbatim to the dashboard caller.
    if parent.is_symlink():
        raise DevcontainerError(
            f"the .devcontainer directory is a symlink, which cannot be "
            f"content-bound to a trust grant: {parent}"
        )

    entries: list[tuple[str, bytes]] = []
    total = 0
    # Counted off the LAZY generator and refused at the cap, so an oversized tree
    # is rejected without ever holding all of it. sorted() on the raw rglob would
    # have to materialize every entry first, and the byte caps below cannot save
    # it: `continue` skips directories before anything is accounted, so a tree of
    # empty directories weighs nothing they measure.
    #
    # Refused rather than truncated on purpose: this tree is content-bound to a
    # trust grant, so hashing only a prefix would leave everything past the cap
    # unscreened while still producing a digest that looks complete.
    candidates: list[Path] = []
    for entry in parent.rglob("*"):
        candidates.append(entry)
        if len(candidates) > _MAX_TREE_ENTRIES:
            raise DevcontainerError(
                f"the .devcontainer tree holds more than {_MAX_TREE_ENTRIES} "
                f"entries, which no hand-maintained config needs; refusing to "
                f"hash a tree that size rather than reading it into memory"
            )
    # Sorted only now, over the bounded list: the digest depends on this order, so
    # the sort cannot be dropped, only made safe to perform.
    for p in sorted(candidates):
        if p.is_symlink():
            raise DevcontainerError(
                f"devcontainer tree contains a symlink, which cannot be "
                f"content-bound to a trust grant: {p}"
            )
        if p.is_dir():
            continue
        # A cheap pre-open reject so an oversized member is refused without
        # opening it at all. It is NOT the enforcing check: the path can be
        # swapped between this stat and the open, so the real per-file ceiling is
        # applied inside _read_config_bytes against the opened fd.
        try:
            size = p.stat().st_size
        except OSError as exc:
            raise DevcontainerError(f"cannot stat devcontainer input {p}: {exc}") from exc
        if size > _MAX_TREE_FILE_BYTES:
            raise DevcontainerError(
                f"devcontainer input {p.name} is {size} bytes, over the "
                f"{_MAX_TREE_FILE_BYTES}-byte per-file limit for a hashed tree"
            )
        # Every member goes through the hardened opener, not a bare
        # read_bytes: these bytes reach the dashboard caller verbatim, so the
        # containment and sensitive-path screens have to gate the whole tree,
        # not just the config file.
        # as_posix, not str: a Windows relpath would hash as "scripts\\x.sh"
        # while the same tree hashes as "scripts/x.sh" elsewhere, making the
        # digest platform-dependent for identical content. The relpath is also
        # shown in the trust prompt, where a forward slash reads correctly on
        # every host.
        rel = p.relative_to(parent).as_posix()
        data = _read_config_bytes(p, _project_root_of(config_path))
        # Accounted from the bytes actually READ, not the pre-open stat, so a
        # tree of files each swapped after their stat cannot sum past the cap.
        total += len(data)
        if total > _MAX_TREE_TOTAL_BYTES:
            raise DevcontainerError(
                f"the .devcontainer tree exceeds the {_MAX_TREE_TOTAL_BYTES}-byte "
                f"total limit; it cannot be hashed for a trust grant"
            )
        entries.append((rel, data))
    return entries


def _digest_entries(entries: list[tuple[str, bytes]], marker: bytes) -> str:
    """Hash an in-memory input set. ``marker`` separates the two layouts so a
    tree and a single file can never collide.

    Every field is LENGTH-PREFIXED rather than NUL-delimited. Delimiting alone is
    ambiguous because file content is arbitrary bytes and may itself contain the
    delimiter: a single file holding ``X\\0Dockerfile\\0RUN ...`` serializes to the
    same stream as two files ``devcontainer.json``=``X`` and
    ``Dockerfile``=``RUN ...``. The two trees then share a digest, so a grant
    approved against the one-file tree also authorizes an unlisted build input the
    human never saw in the prompt. Prefixing each length makes the encoding
    injective, which is the property a content-bound grant depends on.

    The entry count is prefixed for the same reason -- it pins the number of
    members so a set cannot be re-partitioned without changing the digest.
    """
    h = hashlib.sha256()
    h.update(len(entries).to_bytes(8, "big"))
    for rel, data in entries:
        raw_rel = rel.encode()
        h.update(len(raw_rel).to_bytes(8, "big"))
        h.update(raw_rel)
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    h.update(marker)
    return h.hexdigest()


def _parse_jsonc(raw: bytes) -> dict:
    """Parse devcontainer.json, tolerating ``//`` line comments.

    Refuses anything it cannot parse. The containment check below is only sound
    if the config's build inputs can actually be read, so an unparseable config
    must fail closed rather than skip the check. Block comments and trailing
    commas are legal jsonc that this does not handle — such a config is refused
    with a message naming the limitation instead of being silently admitted.

    Also refuses a config too large for the trust prompt to display. The digest
    covers the whole file, so truncating the preview would let a grant authorize
    fields past the cut that the reviewer was never shown.
    """
    if len(raw) > _MAX_PREVIEW_BYTES:
        raise DevcontainerError(
            f"devcontainer.json is {len(raw)} bytes, larger than the "
            f"{_MAX_PREVIEW_BYTES} the trust prompt can display; it cannot be "
            f"reviewed in full, so it is refused rather than trusted in part"
        )
    try:
        obj = json.loads(_LINE_COMMENT_RE.sub("", raw.decode("utf-8", "strict")))
    except (ValueError, UnicodeDecodeError) as exc:
        raise DevcontainerError(
            f"devcontainer.json could not be parsed, so its build inputs "
            f"cannot be verified as digest-bound: {exc}. Remove block comments "
            f"and trailing commas."
        ) from exc
    if not isinstance(obj, dict):
        raise DevcontainerError("devcontainer.json must be a JSON object")
    return obj


def assert_build_inputs_contained(cfg: dict, config_path: Path) -> None:
    """Refuse a config whose build inputs resolve outside the hashed tree.

    The trust digest covers ``.devcontainer/``. A value like
    ``"build": {"dockerfile": "../Dockerfile"}`` points the CLI at a file the
    digest never saw, so editing it later changes what the build executes under
    a still-valid grant. Rather than trying to hash an open-ended set of
    referenced paths (they can reference further paths in turn), the config is
    required to keep every build input inside the tree that IS hashed.
    """
    parent = config_path.parent.resolve()
    if parent.name != ".devcontainer":
        # Root layout hashes one file, so it cannot contain a Dockerfile tree.
        # Any build input at all would be unhashed.
        if _collect_build_inputs(cfg):
            raise DevcontainerError(
                "a root-level .devcontainer.json cannot declare build inputs: "
                "only a .devcontainer/ directory is content-bound to the trust "
                "grant. Move the configuration into .devcontainer/."
            )
        return
    for value in _collect_build_inputs(cfg):
        target = (parent / value).resolve()
        if target != parent and parent not in target.parents:
            raise DevcontainerError(
                f"devcontainer build input {value!r} resolves outside "
                f".devcontainer/ ({target}); it would not be covered by the "
                f"trust digest. Move it inside .devcontainer/."
            )


def _collect_build_inputs(cfg: dict) -> list[str]:
    """Every build-input path the config names, flattened to strings."""
    found: list[str] = []
    build = cfg.get("build")
    if isinstance(build, dict):
        for key in ("dockerfile", "context"):
            v = build.get(key)
            if isinstance(v, str) and v.strip():
                found.append(v.strip())
    # `dockerfile` is also accepted at the top level by the spec's older shape.
    for key in ("dockerfile", "dockerComposeFile"):
        v = cfg.get(key)
        if isinstance(v, str) and v.strip():
            found.append(v.strip())
        elif isinstance(v, list):
            found.extend(x.strip() for x in v if isinstance(x, str) and x.strip())
    return found


# The one lifecycle hook the spec runs on the HOST rather than in the container
# (containers.dev: "run on the host machine during initialization").
_HOST_LIFECYCLE_KEY = "initializeCommand"


def assert_compose_build_inputs_contained(
    cfg: dict,
    entries: list[tuple[str, bytes]] | None,
) -> None:
    """Refuse any compose service whose build inputs escape the hashed tree.

    Run AFTER the sensitive-path screen, so a build input pointing at a sensitive
    location keeps that screen's more specific refusal; this covers what that
    screen accepts -- an input that is not sensitive but is still outside the
    ``.devcontainer/`` tree the digest hashes.
    """
    ref = cfg.get("dockerComposeFile")
    names = [ref] if isinstance(ref, str) else ref
    if not isinstance(names, list) or not names or entries is None or _yaml is None:
        return
    by_rel = dict(entries)
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        data = by_rel.get(name.strip().lstrip("./"))
        if data is None:
            continue
        try:
            doc = _yaml.safe_load(data.decode("utf-8"))
        except (_yaml.YAMLError, UnicodeDecodeError):
            # Screening already refused an unparseable file; nothing to add.
            continue
        if not isinstance(doc, dict):
            continue
        services = doc.get("services")
        if not isinstance(services, dict):
            continue
        for svc in services.values():
            if isinstance(svc, dict):
                _assert_compose_build_inputs_contained(svc, name)
                _assert_compose_service_unprivileged(svc, name)


def _collect_compose_host_binds(
    cfg: dict,
    entries: list[tuple[str, bytes]] | None,
    config_dir: str | Path,
) -> list[str]:
    """Host-side sources of every bind declared in a referenced compose file.

    A compose service's ``volumes:`` never appear in devcontainer.json, so
    screening only the json would leave that whole surface unscreened while the
    compose file is nonetheless frozen and built.

    Fails CLOSED on a compose file this cannot read: an unparseable compose file
    is one whose binds cannot be enumerated, and admitting it would mean building
    from a file whose host access is unknown. ``entries`` is the digest-verified
    tree, so the bytes screened are the bytes that will be built.
    """
    ref = cfg.get("dockerComposeFile")
    names = [ref] if isinstance(ref, str) else ref
    if not isinstance(names, list) or not names:
        return []
    if entries is None:
        # No tree to read from (a caller screening a bare config dict). The json
        # surface is still screened; the compose surface is checked wherever the
        # tree is available, which is every gate that can lead to a build.
        return []

    by_rel = dict(entries)
    if _yaml is None:
        # Refuse rather than skip: without a parser the binds cannot be
        # enumerated, and a compose file whose host access is unknown must not
        # be built.
        raise DevcontainerError(
            "a compose-based devcontainer cannot be screened without a YAML "
            "parser; install pyyaml or use a Dockerfile-based config"
        )

    found: list[str] = []
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        per_file: list[str] = []
        data = by_rel.get(name.strip().lstrip("./"))
        if data is None:
            # Containment already refuses references outside the hashed tree, so
            # reaching here means the reference is unresolvable -- refuse rather
            # than skip, or an unscreened compose file would build.
            raise DevcontainerError(
                f"compose file {name!r} is not part of the hashed devcontainer "
                f"tree, so its bind mounts cannot be screened"
            )
        try:
            doc = _yaml.safe_load(data.decode("utf-8", "strict"))
        except (_yaml.YAMLError, UnicodeDecodeError) as exc:
            raise DevcontainerError(
                f"compose file {name!r} could not be parsed, so its bind mounts "
                f"cannot be screened: {exc}"
            ) from exc
        if doc is None:
            continue
        if not isinstance(doc, dict):
            raise DevcontainerError(
                f"compose file {name!r} is not a mapping, so its bind mounts " f"cannot be screened"
            )
        services = doc.get("services")
        if isinstance(services, dict):
            for svc in services.values():
                if not isinstance(svc, dict):
                    continue
                per_file += _compose_service_host_paths(svc)
        # Surfaces OUTSIDE services, each of which reaches a host path without
        # ever appearing in a service's `volumes`:
        #
        #  * top-level `volumes:` with `driver_opts.device` -- a NAMED volume
        #    that is really a bind. The service side reads `creds:/root/.aws`,
        #    which the screen correctly treats as a name with no host side; the
        #    host path lives only in this definition, so screening services
        #    alone misses it entirely.
        #  * `secrets:` / `configs:` with `file:` -- read from the host and
        #    mounted into the container by the runtime.
        per_file += _compose_top_level_host_paths(doc)
        # Resolved HERE, against this file's own directory, rather than by the
        # caller against one shared base: a nested `sub/compose.yml` resolves its
        # relative paths against `sub/`, so a single base screened the wrong host
        # directory for every file outside the config's own.
        file_dir = _compose_file_dir(config_dir, name)
        for path in per_file:
            if not path:
                continue
            expanded = _expand_devcontainer_vars(path, file_dir)
            if "$" in expanded or _looks_like_named_volume(expanded):
                # Left as-is so the caller's unresolved-variable refusal and
                # named-volume skip still see them unchanged.
                found.append(path)
            elif os.path.isabs(expanded):
                found.append(expanded)
            else:
                found.append(os.path.join(file_dir, expanded))
    return [f for f in found if f]


def _looks_like_named_volume(spec: str) -> bool:
    """Whether *spec* is a bare compose volume NAME rather than a host path.

    A named volume has no host side to screen; treating one as a relative path
    would resolve it against the compose directory and refuse a benign config.
    """
    return "/" not in spec and "\\" not in spec and not spec.startswith(".")


def _assert_compose_service_unprivileged(svc: dict, service_name: str) -> None:
    """Refuse a compose service that escalates past bind screening.

    The compose twin of :func:`assert_no_privileged_modes`. Same reasoning: these
    are runtime capabilities rather than paths, so there is nothing to screen --
    a service holding any of them can reach host storage the config never names,
    which is what makes the declared-mount screening meaningful in the first
    place.

    Unresolved ``${VAR}`` interpolation in any of these fields is refused first.
    Compose substitutes those at up time, so ``privileged: ${PRIVILEGED}`` is
    neither ``True`` nor safe: a value check reads it as "not privileged" and the
    container then starts privileged. There is no value to screen yet, so the
    refusal is the only answer that is not a guess.
    """
    for key in _PRIVILEGE_COMPOSE_KEYS:
        if key in svc and _is_unresolved_interpolation(svc[key]):
            raise DevcontainerError(
                f"compose service {service_name!r} sets {key!r} from an unresolved "
                f"variable, whose value is not known until the container starts -- "
                f"it cannot be screened, so it is refused rather than read as absent"
            )
    # The build block carries its own capabilities, which no path screen can
    # express: `privileged` elevates the build itself, and `ssh` / `secrets`
    # forward host credential material into it -- the config NAMES that material
    # without containing it, so the digest cannot bind what would actually be used
    # and the build could authenticate or sign as the user.
    build = svc.get("build")
    if isinstance(build, dict):
        if build.get("privileged") is True or _is_unresolved_interpolation(build.get("privileged")):
            raise DevcontainerError(
                f"compose service {service_name!r} requests a privileged build, "
                f"which gives the build the host access that makes the path "
                f"screening in this module unenforceable"
            )
        for cap_key in ("ssh", "secrets"):
            if build.get(cap_key):
                raise DevcontainerError(
                    f"compose service {service_name!r} declares build {cap_key!r}, "
                    f"which forwards host credential material (an SSH agent socket "
                    f"or a secret) into the build. The config names it but never "
                    f"contains it, so the digest cannot bind what would actually be "
                    f"used, and the build could authenticate as the user."
                )
    if svc.get("use_api_socket") is True:
        raise DevcontainerError(
            f"compose service {service_name!r} sets use_api_socket, which mounts the "
            f"container engine's API socket into the container. Control of the engine "
            f"is control of every container on the host, so it outranks the mount "
            f"screening here -- the agent could start a container with any bind it "
            f"likes. Refused for the same reason a bind of the socket path is."
        )
    if svc.get("privileged") is True:
        raise DevcontainerError(
            f"compose service {service_name!r} requests privileged: true, which lets "
            f"the container mount host storage it never declared -- screening the "
            f"config's mounts could not then bound what it reads"
        )
    caps = svc.get("cap_add")
    entries = caps if isinstance(caps, list) else [caps]
    for cap in entries:
        if isinstance(cap, str) and cap.strip().lower() in _MOUNT_CAPABLE_CAPS:
            raise DevcontainerError(
                f"compose service {service_name!r} adds capability {cap.strip()!r}, "
                f"which restores the same mount power as privileged"
            )
    # Device access, by VALUE and not only by interpolation. These two keys were
    # already in _PRIVILEGE_COMPOSE_KEYS, but that tuple drives the unresolved-
    # variable screen alone, so a literal rule sailed through: naming the key in the
    # family is not the same as screening what it says.
    for dev_key in ("devices", "device_cgroup_rules"):
        if svc.get(dev_key):
            raise DevcontainerError(
                f"compose service {service_name!r} declares {dev_key!r}, which grants "
                f"the container access to a host device node. A block device is the "
                f"whole filesystem regardless of which paths the config declares, so "
                f"the mount screening here could not bound what the agent reads."
            )
    # Compose's own names for the namespace modes. Sharing ANY of these namespaces
    # with the host or with a sibling container removes the isolation the grant is
    # premised on -- and the documented deployment runs this gateway in Docker, so a
    # sibling PID namespace can be the gateway's own.
    for key in ("network_mode", "pid", "ipc", "uts", "userns_mode", "cgroup"):
        value = svc.get(key)
        if not isinstance(value, str):
            continue
        mode = value.strip().lower()
        if mode == "host":
            raise DevcontainerError(
                f"compose service {service_name!r} sets {key}: host, which places "
                f"the container in the host namespace -- the isolation the trust "
                f"grant is premised on is not created at all"
            )
        if mode.startswith("container:") or mode.startswith("service:"):
            raise DevcontainerError(
                f"compose service {service_name!r} sets {key}: {value.strip()}, which "
                f"joins another container's namespace instead of creating its own. "
                f"This gateway is documented as running in a container, so the "
                f"namespace joined can be the one supervising this session -- the "
                f"container's root could then signal the gateway itself."
            )


def _compose_build_inputs(svc: dict) -> list[str]:
    """Build-input paths one compose SERVICE names.

    Separate from :func:`_compose_service_host_paths`, which collects paths for
    SENSITIVE-TARGET screening. These same values additionally have to be
    content-bound to the trust grant, which is a different question: a build
    context under the project root is not sensitive, so sensitive-path screening
    passes it, and its Dockerfile is then never hashed.
    """
    found: list[str] = []
    build = svc.get("build")
    if isinstance(build, str) and build.strip():
        # String shorthand IS the context.
        found.append(build.strip())
    elif isinstance(build, dict):
        for key in ("context", "dockerfile"):
            value = build.get(key)
            if isinstance(value, str) and value.strip():
                found.append(value.strip())
        extra = build.get("additional_contexts")
        entries: list[str] = []
        if isinstance(extra, dict):
            entries = [v for v in extra.values() if isinstance(v, str)]
        elif isinstance(extra, list):
            entries = [e.split("=", 1)[1] for e in extra if isinstance(e, str) and "=" in e]
        for value in entries:
            if value.strip() and not value.startswith(_NON_PATH_CONTEXT_PREFIXES):
                found.append(value.strip())
    return found


def _assert_compose_build_inputs_contained(svc: dict, compose_name: str) -> None:
    """Refuse a compose service whose build inputs escape the hashed tree.

    The same rule :func:`assert_build_inputs_contained` applies to
    devcontainer.json, applied to the compose file's own ``build:``. Without it
    the json surface was content-bound while the compose surface was not, so
    ``build: {context: ..}`` put a project-root Dockerfile outside the digest:
    editing it afterwards changed what the build executes while the grant stayed
    valid, which is the one thing the digest exists to prevent.

    Checked lexically against the compose file's own relative directory, because
    that is what compose resolves against and because the digest tree is keyed by
    exactly these relative paths -- no filesystem access, so it cannot disagree
    with what was hashed.
    """
    rel_dir = posixpath.dirname(compose_name.strip().lstrip("./"))
    for value in _compose_build_inputs(svc):
        spelled = value.replace("\\", "/")
        if posixpath.isabs(spelled) or _DRIVE_RE.match(spelled):
            raise DevcontainerError(
                f"compose build input {value!r} in {compose_name!r} is an absolute "
                f"path, so it is outside the .devcontainer/ tree the trust grant "
                f"covers. Move it inside .devcontainer/."
            )
        target = posixpath.normpath(posixpath.join(rel_dir, spelled))
        if target == ".." or target.startswith("../"):
            raise DevcontainerError(
                f"compose build input {value!r} in {compose_name!r} resolves "
                f"outside .devcontainer/; it would not be covered by the trust "
                f"digest, so a later edit would change what the build runs under "
                f"an unchanged grant. Move it inside .devcontainer/."
            )


def _compose_service_host_paths(svc: dict) -> list[str]:
    """Host paths one compose SERVICE reaches, across every spelling.

    Raises when the service reaches compose content that trust cannot cover --
    see ``extends`` below.
    """
    found: list[str] = []
    # `extends.file` is refused rather than screened, and the distinction
    # matters: it pulls in a service definition from ANOTHER compose file, which
    # may sit outside `.devcontainer/` and therefore outside the hashed tree. Its
    # own volumes, env_file and build stanzas would then take effect while
    # contributing nothing to the digest, so the human's grant would be bound to
    # content that does not describe what actually gets built -- and editing the
    # extended file afterwards would not invalidate the grant. Screening the
    # paths inside it would not fix that; only refusing does.
    extends = svc.get("extends")
    if isinstance(extends, dict) and isinstance(extends.get("file"), str):
        raise DevcontainerError(
            f"compose service extends another file ({extends['file']!r}), whose "
            "contents cannot be covered by the trust digest; inline the "
            "definition into the .devcontainer tree to use it"
        )
    # Compose's spelling of --volumes-from. Same reasoning: it names a container
    # or service whose mounts are not describable from this config, so there is
    # nothing to screen and the grant could not cover what it inherits.
    if svc.get("volumes_from"):
        raise DevcontainerError(
            "compose service uses 'volumes_from', which inherits another "
            "container's mounts; those cannot be screened from this config, so "
            "the trust grant could not cover them"
        )
    # env_file reads a host file and injects its contents as the service's
    # environment, so a sensitive target hands the in-container agent those
    # credentials without any bind appearing in volumes.
    raw_env_files = svc.get("env_file")
    for entry in raw_env_files if isinstance(raw_env_files, list) else [raw_env_files]:
        if isinstance(entry, str):
            found.append(entry)
        elif isinstance(entry, dict):
            # Long form: {path: ..., required: bool}
            path = entry.get("path")
            if isinstance(path, str):
                found.append(path)
    # A build context is read by the daemon and its contents are available to
    # every COPY in the Dockerfile, so a context of $HOME puts credentials in
    # the image the agent then runs. `build:` also has a string shorthand.
    build = svc.get("build")
    if isinstance(build, str):
        found.append(build)
    elif isinstance(build, dict):
        for key in ("context", "dockerfile"):
            value = build.get(key)
            if isinstance(value, str):
                found.append(value)
        # additional_contexts declares EXTRA build contexts, each read by the
        # daemon exactly like `context` and reachable from any COPY --from, so it
        # needs the same screening. Accepts both the mapping form
        # ({name: path}) and the list form (["name=path"]). Values naming
        # another service, build target, image or URL are not host paths and are
        # skipped -- screening them would refuse benign configs.
        extra = build.get("additional_contexts")
        entries: list[str] = []
        if isinstance(extra, dict):
            entries = [v for v in extra.values() if isinstance(v, str)]
        elif isinstance(extra, list):
            entries = [e.split("=", 1)[1] for e in extra if isinstance(e, str) and "=" in e]
        for value in entries:
            if not value.startswith(_NON_PATH_CONTEXT_PREFIXES):
                found.append(value)
    vols = svc.get("volumes")
    if isinstance(vols, list):
        for vol in vols:
            if isinstance(vol, dict):
                # Long form: {type: bind, source: ..., target: ...}
                src = vol.get("source")
                if isinstance(src, str):
                    found.append(src)
            elif isinstance(vol, str):
                # Short form "host:container[:opts]" -- same drive-letter
                # hazard as docker -v, so it reuses that splitter.
                found.append(_volume_host_part(vol))
    # Compose's own spelling of --device. Screening the docker flag alone left
    # this open: the host node is the first colon-separated component, and /dev
    # is already a refused control tree, so collecting it is the whole fix.
    devices = svc.get("devices")
    if isinstance(devices, list):
        for dev in devices:
            if isinstance(dev, str):
                found.append(_volume_host_part(dev))
            elif isinstance(dev, dict) and isinstance(dev.get("source"), str):
                found.append(dev["source"])
    return found


#: Prefixes marking an `additional_contexts` value that is NOT a host path.
#: BuildKit lets a named context point at another service, a build target, an
#: image, a git remote or a URL; only the remaining (path) case needs screening,
#: and treating these as paths would refuse ordinary configs.
_NON_PATH_CONTEXT_PREFIXES = (
    "service:",
    "target:",
    "docker-image://",
    "oci-layout://",
    "https://",
    "http://",
    "git@",
    "ssh://",
)


def _compose_top_level_host_paths(doc: dict) -> list[str]:
    """Host paths declared outside any service definition.

    Raises on ``include``, for the same reason ``extends.file`` is refused: it
    pulls in whole compose files that may sit outside the hashed tree, so their
    services take effect while contributing nothing to the digest. Screening
    paths inside the including file cannot cover content it merely references.
    """
    if "include" in doc:
        raise DevcontainerError(
            "compose file uses top-level 'include', which pulls in compose "
            "content that cannot be covered by the trust digest; inline the "
            "included services into the .devcontainer tree to use it"
        )
    found: list[str] = []
    volumes = doc.get("volumes")
    if isinstance(volumes, dict):
        for definition in volumes.values():
            if not isinstance(definition, dict):
                continue
            device = definition.get("driver_opts", {})
            if isinstance(device, dict) and isinstance(device.get("device"), str):
                found.append(device["device"])
    for section in ("secrets", "configs"):
        entries = doc.get(section)
        if not isinstance(entries, dict):
            continue
        for definition in entries.values():
            if isinstance(definition, dict) and isinstance(definition.get("file"), str):
                found.append(definition["file"])
    return found


#: Closed screen, class "named host-control escapes". Binding any of these is
#: an ESCAPE, not a read: with the docker socket the agent can ask the host
#: daemon for a fresh container mounting anything, which walks around every
#: path restriction. They are not credential paths, so ``is_sensitive_path``
#: does not cover them. A newly spotted socket *location* belongs in this
#: class (leaf-name match already covers rootless homes). It is not a sixth
#: class.
_HOST_CONTROL_PATHS = (
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/var/run/podman/podman.sock",
    "/run/podman/podman.sock",
    "/run/containerd/containerd.sock",
    "/var/run/containerd/containerd.sock",
    "/run/crio/crio.sock",
)

#: Runtime API sockets, wherever they live. Rootless Docker and Podman put
#: ``docker.sock`` / ``podman.sock`` under ``${XDG_RUNTIME_DIR}`` (typically
#: ``/run/user/<uid>``), which is a sibling of the rootful paths above — an
#: equality-or-ancestor check against those paths cannot see it. Matching the
#: leaf name closes every location, including a custom ``XDG_RUNTIME_DIR``.
_HOST_CONTROL_SOCKET_NAMES = frozenset(
    {"docker.sock", "podman.sock", "containerd.sock", "crio.sock"}
)

#: Pseudo-filesystems whose whole point is host-wide visibility and control.
#: ``/run/user`` is the standard rootless-runtime directory: binding it, or
#: ``/run/user/<uid>``, hands over every socket in that tree the same way
#: binding ``/run`` hands over ``/run/docker.sock``.
_HOST_CONTROL_TREES = ("/proc", "/sys", "/dev", "/run/user")


#: A Windows drive prefix. Matched explicitly rather than via ``os.path.splitdrive``,
#: which is platform-dependent: on POSIX it does not recognize drives at all, so a
#: Windows-shaped path would pass through unnormalized on Linux.
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _canonical_for_control_match(candidate: str) -> str:
    """Normalize a path for comparison against the host-control lists.

    ``devcontainer.json`` describes a LINUX container, so its bind sources are
    written POSIX-style (``/var/run/docker.sock``) whatever the host OS. On
    Windows ``realpath`` rewrites that to ``C:\\var\\run\\docker.sock``, so a
    literal comparison would miss the socket on exactly the platform where
    ``os.path.isabs`` also fails to recognize it.
    """
    norm = candidate.replace("\\", "/")
    norm = _DRIVE_RE.sub("", norm) or "/"
    while "//" in norm:
        norm = norm.replace("//", "/")
    return norm.rstrip("/") or "/"


def _is_container_absolute(source: str) -> bool:
    """True when a bind source names an absolute host path.

    ``os.path.isabs`` answers for the HOST's syntax, which is wrong here: on
    Windows it rejects ``/var/run/docker.sock``, so a POSIX-style source -- the
    only style a Linux container spec uses -- would be misread as relative and
    resolved into the project instead of screened. The drive form is checked for
    the mirror case, a Windows-shaped source seen on a POSIX host.
    """
    return (
        os.path.isabs(source)
        or source.startswith(("/", "\\"))
        or _DRIVE_RE.match(source) is not None
    )


def _grants_host_control(resolved: str) -> bool:
    """True when a bind of this path would hand over the host runtime.

    Containment is checked in BOTH directions, which is the whole point:

    - **Descendant** -- binding ``/proc`` or a subtree of it is the same class of
      grant as binding the pseudo-filesystem itself.
    - **Ancestor** -- a source that merely CONTAINS a control path hands over
      everything inside it. ``source=/run`` is not equal to
      ``/run/docker.sock`` and is not under ``/proc``, so an equality-only check
      accepted it and the container got the daemon socket anyway. ``/``,
      ``/var``, ``/var/run`` and ``/run`` are all this same case.

    Both tuples are screened the same way rather than one by equality and the
    other by containment: the asymmetry is what let the ancestor case through.
    """
    norm = _canonical_for_control_match(resolved)
    # Split on the already-canonical separator, not os.path.basename: on
    # Windows basename splits on ``\`` only, so a POSIX-style source would
    # keep its whole path as the "leaf" and this check would miss it.
    if norm.rsplit("/", 1)[-1] in _HOST_CONTROL_SOCKET_NAMES:
        return True
    for entry in _HOST_CONTROL_PATHS + _HOST_CONTROL_TREES:
        control = _canonical_for_control_match(entry)
        if norm == control:
            return True
        if norm.startswith(control + "/"):  # inside a control path
            return True
        if control.startswith(norm.rstrip("/") + "/"):  # contains a control path
            return True
    return False


def _looks_like_relative_path(source: str) -> bool:
    """Distinguish a relative PATH from a docker named volume.

    A named volume is a bare token (``myvol``) with no separator; anything
    carrying ``.`` or a separator is a path compose will resolve against the
    compose file's directory, and therefore something that can escape upward.
    """
    return source.startswith((".", "/", "\\")) or "/" in source or "\\" in source


def _compose_file_dir(config_dir: str | Path, name: str) -> str:
    """Directory a relative path INSIDE compose file *name* resolves against.

    Compose resolves each file's relative paths against THAT file's own
    directory, and ``dockerComposeFile`` may name a subdirectory
    (``sub/compose.yml``). Anchoring every reference at ``.devcontainer/``
    screened and rewrote the wrong host directory for any nested file, so a bind
    checked as one path was built as another.
    """
    return str((Path(config_dir) / name.strip().lstrip("./")).parent)


def _compose_base_dir(cfg: dict, project_dir: str | Path) -> str:
    """Directory a relative compose bind resolves against.

    Compose resolves relative binds against the compose FILE's directory. The
    referenced file is required to live inside ``.devcontainer/``, so that is the
    base; falling back to the project root keeps this defined for a config with
    no compose reference at all.
    """
    ref = cfg.get("dockerComposeFile")
    names = [ref] if isinstance(ref, str) else ref
    if isinstance(names, list) and names:
        return str(Path(project_dir) / ".devcontainer")
    return str(project_dir)


def assert_no_sensitive_host_mounts(
    cfg: dict,
    project_dir: str | Path,
    entries: list[tuple[str, bytes]] | None = None,
) -> None:
    """Refuse a config that binds a sensitive host path into the container.

    The container replaces the host sandbox for a containerized session, because
    ``wrap_argv`` and the cgroup wrapper are host mechanisms that cannot cross
    the boundary. That trade only holds while the container cannot be pointed at
    the very paths the host sandbox exists to hide: a ``mounts`` entry for
    ``~/.aws`` would hand the agent credentials it could not otherwise read, so
    the container would be weaker than the sandbox rather than equivalent.

    Screened with ``is_sensitive_path`` -- the same predicate that gates config
    reads -- across every shape that can express a host bind: ``mounts`` (string
    ``source=...`` form and object form), ``workspaceMount``, the raw docker
    flags in ``runArgs``, and, when ``entries`` carries the hashed tree, the
    ``volumes:`` of every service in a referenced compose file. Compose matters
    because a bind declared there never appears in devcontainer.json at all, so
    screening only the json would leave the whole compose surface open.
    ``${localEnv:VAR}``, ``${localWorkspaceFolder}`` and plain ``$VAR`` are
    expanded first, since the escape would otherwise just be spelled with a
    variable.

    Not a substitute for the trust prompt: the human still approves the config.
    It removes the case where approving something that looks routine silently
    waives a protection the operator declared separately.
    """
    from kiro_crew.security import (  # circular import
        is_sensitive_path,
        path_contains_sensitive,
    )

    sources = list(_collect_host_mount_sources(cfg))
    # Compose binds come back ABSOLUTE, already resolved against each
    # compose file's own directory; only json-declared paths use `base`.
    sources += _collect_compose_host_binds(cfg, entries, _compose_base_dir(cfg, project_dir))
    for raw_source in sources:
        source = _expand_devcontainer_vars(raw_source, project_dir)
        if "$" in source:
            # An unexpanded variable means the real path is unknown at screening
            # time, so "not sensitive" cannot be concluded. Refuse instead of
            # treating an unresolved source as safe.
            raise DevcontainerError(
                f"devcontainer mount source {raw_source!r} still contains an "
                f"unresolved variable after expansion, so it cannot be screened "
                f"against sensitive host paths"
            )
        if not source:
            continue
        if not _is_container_absolute(source):
            # A bare name is a named volume, which has no host side at all. A
            # RELATIVE PATH is different: compose resolves it against the compose
            # file's directory, so "../../../trust.json" escapes the project and
            # can reach the gateway's own keystone files. Skipping everything
            # non-absolute treated those as harmless.
            if _looks_like_relative_path(source):
                base = _compose_base_dir(cfg, project_dir)
                resolved = os.path.realpath(os.path.join(base, source))
            else:
                continue
        else:
            resolved = os.path.realpath(source)
        # Runtime-control paths are an escape rather than a credential read: a
        # bind of the docker socket lets the agent ask the host daemon for a new
        # container mounting anything at all, which walks around every path check
        # above. They are not "sensitive" in the credential sense, so the
        # path screens below do not see them.
        if _grants_host_control(resolved):
            raise DevcontainerError(
                f"devcontainer config mounts a host control interface into the "
                f"container ({raw_source!r} -> {resolved}); that hands the agent "
                f"the host container runtime, which defeats every other mount "
                f"restriction"
            )
        # Both directions matter. is_sensitive_path answers "is this path INSIDE
        # a protected location", which a bind of an ANCESTOR passes: ``$HOME``
        # and ``/`` are not themselves sensitive entries, yet binding either
        # hands the agent ~/.aws and ~/.ssh. path_contains_sensitive closes that,
        # so the guard holds the invariant its own message states.
        if is_sensitive_path(resolved) or path_contains_sensitive(resolved):
            raise DevcontainerError(
                f"devcontainer config mounts a sensitive host path into the "
                f"container ({raw_source!r} -> {resolved}); the container "
                f"replaces the host sandbox, so it must not expose paths the "
                f"sandbox withholds"
            )
        # Path-valued env we forwarded to the CLI (DOCKER_CERT_PATH, …) is the
        # same class: ${localEnv:VAR} expands to a custom location that is not
        # in any fixed sensitive-path list.
        if _is_forwarded_secret_path(resolved):
            raise DevcontainerError(
                f"devcontainer config mounts a forwarded credential path into "
                f"the container ({raw_source!r} -> {resolved}); that path was "
                f"handed to the build so the daemon can authenticate, not so "
                f"the agent can read it"
            )
    # Last, so a build input pointing somewhere sensitive keeps the more specific
    # refusal above. What remains for this check is the case that screen accepts:
    # an input that is not sensitive yet still resolves outside the hashed tree,
    # so a later edit would change what the build runs under an unchanged grant.
    assert_compose_build_inputs_contained(cfg, entries)
    # Last: a privileged container makes every check above unenforceable, so
    # refusing it is what keeps their verdicts meaningful.
    assert_no_privileged_modes(cfg)
    # Checked alongside the privileged screen because it is the same hole from
    # the other side: a Feature can carry what that screen refuses, in metadata
    # this config does not contain.
    assert_no_unscreened_features(cfg)
    # The workspace bind is implicit, so it is screened here by refusing the
    # project root rather than by inspecting a mount entry that never exists.
    assert_project_dir_mountable(project_dir)


def _expand_devcontainer_vars(value: str, project_dir: str | Path) -> str:
    """Expand the variable shapes that can name a host path.

    Covers the devcontainer spec's ``${localEnv:VAR}`` and
    ``${localWorkspaceFolder}`` plus compose's ``${VAR}``, ``${VAR:-default}``,
    ``${VAR-default}`` and bare ``$VAR``, because a bind spelled
    ``${HOME}/.aws`` must screen the same as the literal path.

    A variable this cannot resolve is left UNEXPANDED rather than substituted
    with the empty string. Two reasons, both fail-closed:

    * compose also interpolates from the project's ``.env`` file, which is not
      read here -- so an unset-to-us variable may well be set for the build, and
      collapsing it to empty would screen a path that is not the one docker
      mounts;
    * an empty substitution makes the source non-absolute, which the caller then
      skips as "not a host path" -- silently declining to screen.

    Leaving the ``$`` in place hands the value to the caller's
    unresolved-variable guard, which refuses it.
    """
    out = value.replace("${localWorkspaceFolder}", str(project_dir))

    def _local_env(m: re.Match[str]) -> str:
        # Unset stays literal so the unresolved guard sees it.
        return os.environ.get(m.group(1), m.group(0))

    out = re.sub(r"\$\{localEnv:([A-Za-z_][A-Za-z0-9_]*)\}", _local_env, out)

    def _compose_var(m: re.Match[str]) -> str:
        # A resolvable value is used; anything else stays literal. A default is
        # only what compose falls back to when NOTHING supplies the variable,
        # and the project's .env may -- so screening the default would screen a
        # guess rather than the path docker mounts.
        return os.environ.get(m.group(1)) or m.group(0)

    out = re.sub(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])([^}]*))?\}",
        _compose_var,
        out,
    )
    return re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", _local_env, out)


#: runArgs flags that consume a value. Needed to decide whether the NEXT token is
#: this flag's value or a flag of its own; docker's grammar is not self-describing.
_RUN_VALUE_FLAGS = frozenset(
    {
        "-v",
        "--volume",
        "--device",
        "--mount",
        "--volumes-from",
        "--memory",
        "--memory-swap",
        "--pids-limit",
        "--cpus",
        # Host-file flags take a value too; without them here the separate-token
        # spelling (`--env-file PATH`) would leave PATH unclaimed.
        "--env-file",
        "--label-file",
        "--cidfile",
        # Privileged-mode screening reads these; without them the separate-token
        # spelling (`--cap-add SYS_ADMIN`) would leave the value unclaimed.
        "--cap-add",
        # Same reason, and the reason it matters here: the device screen tests the
        # VALUE, so an unclaimed one reads as absent and the screen never fires.
        "--device-cgroup-rule",
        "--pid",
        "--network",
        "--ipc",
        "--uts",
        "--userns",
        "--cgroupns",
    }
)

#: Short and alias spellings docker accepts for a long flag this module
#: screens. One map, so a screen that understands ``--network=host`` also
#: understands ``--net=host`` (and ``-v`` already covered ``--volume``).
#: Only aliases of flags we actually consume belong here — adding a spelling
#: whose long form is never read is not coverage.
_RUN_FLAG_CANON = {
    "-v": "--volume",
    "--net": "--network",
    "-m": "--memory",
}


def _split_run_arg(arg: str) -> tuple[str, str | None]:
    """Split one runArgs token into ``(flag, attached value or None)``.

    Handles every spelling docker accepts for an attached value, which is the
    point: a screen that understood only ``--flag=value`` missed
    ``-v/var/run/docker.sock:/sock`` entirely, because a short flag may carry its
    value with NO separator at all.
    """
    if arg.startswith("--"):
        name, sep, val = arg.partition("=")
        return name, (val if sep else None)
    if arg.startswith("-") and len(arg) > 1:
        name, rest = arg[:2], arg[2:]
        if not rest:
            return name, None
        # `-v=X` and `-vX` both attach X to -v.
        return name, (rest[1:] if rest.startswith("=") else rest)
    return arg, None


def _iter_run_flags(flags: list[str]) -> list[tuple[str, str | None]]:
    """Every ``(canonical_flag, value)`` pair in *flags*.

    One grammar, one place. Consumers ask what a flag is set to instead of
    re-deriving docker's spelling rules, which is what let each fix in this area
    close a single spelling and leave its siblings reachable.
    """
    out: list[tuple[str, str | None]] = []
    i = 0
    while i < len(flags):
        raw = flags[i]
        name, val = _split_run_arg(raw)
        canon = _RUN_FLAG_CANON.get(name, name)
        if val is None and canon in _RUN_VALUE_FLAGS and i + 1 < len(flags):
            val = flags[i + 1]
            i += 1
        out.append((canon, val))
        i += 1
    return out


#: Capabilities that restore the mount power ``--privileged`` grants. SYS_ADMIN
#: is mount(2); ALL includes it. Screening binds is pointless against either.
_MOUNT_CAPABLE_CAPS = frozenset({"sys_admin", "all"})

#: Namespace modes that put the container in the HOST's namespace, so the
#: isolation the trust grant is premised on is not created at all.
_HOST_NAMESPACE_FLAGS = ("--pid", "--network", "--ipc", "--uts", "--userns", "--cgroupns")

#: runArgs spellings that hand over a host device. `--device` maps a node directly;
#: `--device-cgroup-rule` grants the cgroup permission to create one, which a root
#: agent completes with mknod -- so the rule is the same grant, one step removed.
_DEVICE_FLAGS = ("--device", "--device-cgroup-rule")


def assert_no_unscreened_features(cfg: dict) -> None:
    """Refuse ``features`` (closed screen, digest-integrity class).

    Their metadata is remote, merged, and unscreenable.

    A Feature is a registry reference the CLI resolves at build time and merges
    into the effective config. The merged metadata may declare ``privileged``,
    ``capAdd``, ``mounts`` or ``containerEnv``, so every screen in this module can
    pass on the local text while the container the CLI actually builds carries a
    runtime-socket mount or SYS_ADMIN.

    The trust grant cannot cover it either: the digest binds the local tree, while
    a Feature's contents are whatever the registry serves at build time -- so the
    exact-bytes consent the trust prompt offers would be a promise about text that
    is not what runs.

    Refusing is a real reduction in what the preview accepts, since Features are
    how most devcontainers install toolchains. It is the honest posture until the
    resolved metadata is fetched, screened and hashed like any other input.
    """
    features = cfg.get("features")
    if isinstance(features, dict):
        names = sorted(str(k) for k in features)
    elif isinstance(features, (list, tuple)):
        names = sorted(str(f) for f in features)
    elif features:
        names = [str(features)]
    else:
        return
    if not names:
        return
    raise DevcontainerError(
        "this devcontainer.json declares features ("
        + ", ".join(names[:4])
        + (", ..." if len(names) > 4 else "")
        + "), whose metadata the CLI fetches from a registry and merges into the "
        "config at build time. That metadata can grant privileged access, add "
        "capabilities or mount host paths, so none of the screening on this "
        "config -- or the exact bytes shown at the trust prompt -- describes what "
        "would actually run. Remove the features to use a Dev Container in this "
        "preview."
    )


#: devcontainer.json properties that grant the escalation directly, without going
#: through runArgs. These are first-class spec properties, so a config never has to
#: spell them as flags -- reading only runArgs left the shortest spelling wide open.
_PRIVILEGE_CONFIG_KEYS = ("privileged", "capAdd", "securityOpt")

#: Compose service keys in the same family. Screened by KEY, not by value, for the
#: reason in _is_unresolved_interpolation: compose resolves variables later, so the
#: value visible here is not necessarily the value that runs.
_PRIVILEGE_COMPOSE_KEYS = (
    "privileged",
    "cap_add",
    "security_opt",
    "devices",
    "device_cgroup_rules",
    # A boolean that means "mount the engine API socket". The host-control bind
    # screen already refuses that socket when it is written as a path; this is the
    # same grant spelled as a flag, which is why no path screen could see it.
    "use_api_socket",
)


def _is_unresolved_interpolation(value: object) -> bool:
    """True when *value* still contains a compose ``${VAR}`` reference.

    Compose substitutes these at up time from the environment, so what is written
    in the file is not what the container gets. For an ordinary field that is the
    project's business; for a privilege field it means the screen is being asked to
    decide on a value that does not exist yet, and the honest answer is to refuse
    rather than to read ``${PRIVILEGED}`` as "not true".
    """
    if isinstance(value, str):
        return "${" in value or (value.startswith("$") and len(value) > 1)
    if isinstance(value, (list, tuple)):
        return any(_is_unresolved_interpolation(v) for v in value)
    if isinstance(value, dict):
        return any(_is_unresolved_interpolation(v) for v in value.values())
    return False


def _assert_no_privileged_config_keys(cfg: dict) -> None:
    """Refuse the top-level devcontainer.json privilege properties.

    ``privileged`` and ``capAdd`` are properties of the spec itself, so a config
    can escalate without ever mentioning runArgs. An empty or false value is
    accepted: refusing ``"privileged": false`` would reject a config that is
    explicitly declining the very thing being screened for.
    """
    for key in _PRIVILEGE_CONFIG_KEYS:
        if key not in cfg:
            continue
        value = cfg[key]
        if _is_unresolved_interpolation(value):
            raise DevcontainerError(
                f"devcontainer.json sets `{key}` to an unresolved variable, whose "
                f"value is not known until build time -- it cannot be screened, so "
                f"it is refused rather than assumed harmless"
            )
        if value in (None, False, [], (), "", {}):
            continue
        if key == "privileged":
            raise DevcontainerError(
                "devcontainer.json requests `privileged`, which lets the container "
                "mount host block devices from inside. Every host-path screen here "
                "reads what the config declares, and privileged access makes those "
                "declarations stop bounding what can be read, so it is refused. "
                "This is stricter than VS Code, which honors it."
            )
        raise DevcontainerError(
            f"devcontainer.json requests `{key}`, which can grant the mount "
            f"capability that makes the host-path screening in this module "
            f"unenforceable. Remove it to use a Dev Container in this preview."
        )


def assert_no_privileged_modes(cfg: dict) -> None:
    """Refuse a config that would make bind screening unenforceable.

    Every other host-path check in this module screens what the config DECLARES.
    That is only meaningful while the container cannot reach host storage it did
    not declare. ``--privileged`` (and the capability / namespace spellings of the
    same grant) hands it exactly that: the agent can mount a host block device
    from inside and read anything on it, so a config that declares no sensitive
    path is indistinguishable from one that declares all of them.

    Refused rather than screened, because there is nothing to screen -- the
    escalation is a runtime capability, not a path. This is stricter than VS Code,
    which honors the flag; the difference is that VS Code is not also making a
    written promise about which host paths the container can see.
    """
    # Checked BEFORE the runArgs read, and outside the early return below: the
    # top-level properties need no runArgs to take effect, so a config that omits
    # runArgs entirely used to skip this function's whole body.
    _assert_no_privileged_config_keys(cfg)
    args = cfg.get("runArgs")
    if not isinstance(args, list):
        return
    flags = [a for a in args if isinstance(a, str)]
    for canon, value in _iter_run_flags(flags):
        if canon == "--privileged" and (value is None or value.strip().lower() != "false"):
            raise DevcontainerError(
                "devcontainer runArgs use --privileged, which lets the container "
                "mount host storage it never declared -- so screening the config's "
                "mounts could not tell you what it can read. Remove it, or grant "
                "only the specific --device the container needs."
            )
        if canon == "--cap-add" and value and value.strip().lower() in _MOUNT_CAPABLE_CAPS:
            raise DevcontainerError(
                f"devcontainer runArgs add capability {value.strip()!r}, which "
                f"restores the same mount power as --privileged, so the config's "
                f"declared mounts would no longer bound what the container reads"
            )
        if canon in _HOST_NAMESPACE_FLAGS and value:
            mode = value.strip().lower()
            if mode == "host":
                raise DevcontainerError(
                    f"devcontainer runArgs set {canon}=host, which places the "
                    f"container in the host namespace -- the isolation the trust "
                    f"grant is premised on is not created at all"
                )
            # A sibling namespace is not the host's, and is not safe either: the
            # documented deployment runs this gateway in a container, so the
            # namespace being joined can be the one supervising this session.
            if mode.startswith("container:"):
                raise DevcontainerError(
                    f"devcontainer runArgs set {canon}={value.strip()}, which joins "
                    f"another container's namespace instead of creating its own. "
                    f"This gateway is documented as running in a container, so that "
                    f"namespace can be the one supervising this session."
                )
        # Device access is a capability, not a path, so no mount screen sees it: a
        # host block device node exposes the entire filesystem whatever the config
        # declares.
        if canon in _DEVICE_FLAGS and value:
            raise DevcontainerError(
                f"devcontainer runArgs set {canon}={value.strip()}, which grants the "
                f"container access to a host device node. A block device is the whole "
                f"filesystem regardless of the paths the config declares."
            )


def _collect_host_mount_sources(cfg: dict) -> list[str]:
    """Every host-side path a config's mount directives can name."""
    found: list[str] = []

    def add_mount(entry: object) -> None:
        if isinstance(entry, dict):
            src = entry.get("source")
            if isinstance(src, str):
                found.append(src)
            return
        if not isinstance(entry, str):
            return
        # "source=/host/path,target=/in/container,type=bind" in any field order.
        for part in entry.split(","):
            k, _, v = part.partition("=")
            if k.strip() in ("source", "src") and v.strip():
                found.append(v.strip())
            # A local-driver volume carrying `volume-opt=device=<path>` with
            # `o=bind` IS a bind: the host path lives in the option, never in
            # `source`, so collecting only source/src let it through. Same shape
            # as compose's `driver_opts.device`, which is already screened -- this
            # is that hazard's runArgs/--mount spelling. The value is nested
            # (`volume-opt=device=/run`), so it needs a second partition; a single
            # split leaves the path glued to the option name and unscreenable.
            if k.strip() in ("volume-opt", "driver-opt") and v.strip():
                opt, _, dev = v.partition("=")
                if opt.strip() == "device" and dev.strip():
                    found.append(dev.strip())

    raw_mounts = cfg.get("mounts")
    for entry in raw_mounts if isinstance(raw_mounts, list) else [raw_mounts]:
        add_mount(entry)
    add_mount(cfg.get("workspaceMount"))

    args = cfg.get("runArgs")
    if isinstance(args, list):
        flags = [a for a in args if isinstance(a, str)]
        for canon, value in _iter_run_flags(flags):
            # --volumes-from names a CONTAINER, not a path, so there is nothing to
            # screen: it inherits whatever that container mounted, which may
            # include every path this screen exists to refuse. Refused rather than
            # collected for the same reason as extends.file -- the content it
            # reaches is not describable from the config the human approved.
            if canon == "--volumes-from":
                raise DevcontainerError(
                    "devcontainer runArgs use --volumes-from, which inherits "
                    "another container's mounts; those cannot be screened from "
                    "this config, so the grant could not cover them"
                )
            # -v/--volume take "host:container"; --mount takes the kv form. Every
            # spelling arrives here already split, so none can be missed.
            if canon == "--volume" and value:
                found.append(_volume_host_part(value))
            # --device hands the container a host device node. The host side is
            # the first colon-separated component, exactly as for -v, so it
            # reuses that splitter -- and /dev is already a screened control
            # tree, which means collecting the path is the entire fix. A screen
            # that only understood bind syntax never saw this flag at all, so
            # `--device=/dev/kmsg` reached the daemon unexamined.
            elif canon == "--device" and value:
                # A host device node. The host side is the first colon-separated
                # component exactly as for -v, so it reuses that splitter -- and
                # /dev is already a screened control tree, so collecting the path
                # is the entire fix.
                found.append(_volume_host_part(value))
            elif canon == "--mount" and value:
                add_mount(value)
            # These read a host file WITHOUT mounting it, so a screen that only
            # understood bind syntax let them through: --env-file pointing at the
            # gateway's own .env hands the container every credential in it, and
            # kiro-cli inside then inherits them. The path is the payload here
            # even though nothing is bound.
            elif canon in _HOST_FILE_READING_FLAGS and value:
                found.append(value.strip())
    return [f for f in found if f]


#: docker flags whose VALUE is a host path the daemon reads on the container's
#: behalf, without any bind mount appearing in the config. They need the same
#: sensitive-path screening as a bind: ``--env-file ~/.kiro/crew/.env`` copies
#: every credential in that file into the container's environment, which
#: kiro-cli then inherits, and no mount syntax is involved at any point.
_HOST_FILE_READING_FLAGS = (
    "--env-file",
    "--label-file",
    "--cidfile",
)


def _volume_host_part(spec: str) -> str:
    """The host side of a docker ``-v host:container[:opts]`` spec.

    A bare ``split(":", 1)`` is wrong on Windows, where the host path carries a
    drive letter: ``C:\\Users\\me\\.aws:/root/.aws`` would yield ``"C"``, which
    is not a path, so the bind would silently escape screening -- and that is
    the spelling docker uses on Windows. A leading ``<letter>:`` followed by a
    separator is therefore treated as part of the path, and the split looks for
    the next colon after it.
    """
    if re.match(r"^[A-Za-z]:[\\/]", spec):
        rest = spec[2:]
        idx = rest.find(":")
        return spec[:2] + (rest if idx == -1 else rest[:idx])
    return spec.split(":", 1)[0]


#: Environment the devcontainer CLI is given, by exact name. An allowlist rather
#: than a denylist because the build environment is where a trusted config can
#: reach a host credential through ``${localEnv:VAR}``: a denylist admits every
#: name nobody thought to enumerate, and `KIRO_API_KEY` was exactly that case.
#:
#: Each group is here for a stated reason, so a future addition is a decision:
#:   * PATH/HOME/USER/LOGNAME/TMPDIR/TERM -- the CLI resolves docker and node from
#:     PATH and reads ~/.docker; the rest keep ordinary tooling from erroring out.
#:     PATH is then filtered to directories this process cannot write, so a
#:     planted ``node``/``docker`` shim in a worktree venv cannot ride along.
#:   * XDG_RUNTIME_DIR -- where a rootless docker socket lives.
#:   * DOCKER_* -- a non-default daemon endpoint and its TLS material.
#:   * LANG/LC_* -- without a locale the CLI can mis-decode build output.
#:   * proxy + CA-bundle names -- image pulls fail closed on a corporate network
#:     without them. A proxy URL CAN embed a credential; that is the user's own
#:     proxy going to the user's own container, which is a far smaller exposure
#:     than the gateway's credentials and the alternative is an unusable feature.
_BUILD_ENV_ALLOWED = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "TERM",
        "XDG_RUNTIME_DIR",
        "DOCKER_HOST",
        "DOCKER_CONFIG",
        "DOCKER_CERT_PATH",
        "DOCKER_TLS_VERIFY",
        "DOCKER_BUILDKIT",
        "LANG",
        "LC_ALL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "CURL_CA_BUNDLE",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    }
)

#: Locale variables beyond LC_ALL (LC_CTYPE, LC_MESSAGES, ...) are admitted by
#: prefix rather than enumerated -- they are a fixed, credential-free family.
_BUILD_ENV_ALLOWED_PREFIXES = ("LC_",)

#: Forwarded names whose VALUE is a host path of credential or TLS material.
#: A trusted config can name them via ``${localEnv:VAR}`` as a bind source,
#: which is the same grant as mounting the path literally — and a custom
#: ``DOCKER_CERT_PATH`` is not in any fixed sensitive-path list. Screened by
#: the live value, so every location is closed, not one spelling.
_BUILD_ENV_SECRET_PATHS = frozenset(
    {
        "DOCKER_CONFIG",
        "DOCKER_CERT_PATH",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "CURL_CA_BUNDLE",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    }
)


def _trusted_path() -> str:
    """``PATH`` containing only directories the agent cannot write.

    The CLI resolves ``node`` and ``docker`` from the environment we hand it.
    Verifying the *argv* binary is not enough: a writable PATH entry lets the
    agent plant a shim the trusted CLI then execs on the host, as this uid.
    """
    kept: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        if _agent_writable(entry) is None:
            kept.append(entry)
    return os.pathsep.join(kept)


def _is_forwarded_secret_path(resolved: str) -> bool:
    """True when *resolved* is (or contains) a secret path we forwarded.

    Containment is checked both ways, matching ``_grants_host_control``: binding
    the cert directory, a file inside it, or an ancestor that contains it, is
    the same grant.
    """
    norm = _canonical_for_control_match(resolved)
    for name in _BUILD_ENV_SECRET_PATHS:
        raw = os.environ.get(name)
        if not raw:
            continue
        control = _canonical_for_control_match(os.path.realpath(raw))
        if norm == control:
            return True
        if norm.startswith(control + "/"):
            return True
        if control.startswith(norm.rstrip("/") + "/"):
            return True
    return False


def _scrubbed_build_env() -> dict[str, str]:
    """The environment the devcontainer CLI is allowed to see.

    Uses the FULL scrub, not the parent-level channel-credential scrub. The
    weaker one deliberately preserves AWS/SSH/GPG because the host ``standard``
    sandbox is designed to keep git-over-SSH, the AWS CLI and kubectl working --
    the agent runs as this uid there and could read those anyway. Neither
    premise holds here: this environment is handed to ``devcontainer up``, so a
    trusted config naming ``${localEnv:AWS_SECRET_ACCESS_KEY}`` in
    ``containerEnv``/``remoteEnv`` materializes the GATEWAY's credential inside
    the container, and build-time and in-container lifecycle commands then see
    it. The container exists to be a confinement boundary, so the environment
    crossing it starts from the stricter set.

    ``scrub_env``'s prefix list is a strict superset of the channel-credential
    one, so nothing the weaker scrub removed survives this.

    Consequence worth knowing: an image pulled from a registry whose credential
    helper reads AWS environment variables (ECR) will not authenticate from this
    environment. That is a visible failure with a fixable cause, which is the
    right trade against silently copying a live secret into the container.

    Imported lazily because ``sandbox`` reaches back into this package's config
    layer; the same reason ``is_sensitive_path`` is imported at its call site.
    """
    from kiro_crew.sandbox import scrub_env

    kept = {
        k: v
        for k, v in os.environ.items()
        if k in _BUILD_ENV_ALLOWED or k.startswith(_BUILD_ENV_ALLOWED_PREFIXES)
    }
    # The denylist stays BEHIND the allowlist, not in place of it: if a name is
    # ever added above that turns out to be credential-bearing, the known
    # patterns are still stripped rather than shipped.
    env = scrub_env(kept)
    if "PATH" in env:
        trusted = _trusted_path()
        if trusted:
            env["PATH"] = trusted
        else:
            env.pop("PATH", None)
    return env


def assert_project_dir_mountable(project_dir: str | Path) -> None:
    """Refuse a project whose directory the container must not be handed.

    The devcontainer CLI bind-mounts ``--workspace-folder`` into the container as
    a matter of course, and that mount appears in no config, so none of the
    declared-mount screening in this module can see it. A project rooted at the
    home directory therefore delivers ``~/.aws``, ``~/.ssh`` and Kiro Crew's own
    keystone through the one bind nobody declared.

    Both directions are checked, for the same reason the bind screen checks both:
    ``is_sensitive_path`` answers "is this inside a protected location", which a
    project that CONTAINS one passes, and ``path_contains_sensitive`` closes that.
    """
    from kiro_crew.security import (  # circular import
        is_sensitive_path,
        path_contains_sensitive,
    )

    resolved = os.path.realpath(str(project_dir))
    if is_sensitive_path(resolved) or path_contains_sensitive(resolved):
        raise DevcontainerError(
            f"the project directory {resolved} is, or contains, a path the agent "
            f"sandbox withholds -- the container's workspace mount would hand it "
            f"over, and that mount is implicit so no config screening covers it. "
            f"Move the project outside protected locations to use a Dev Container."
        )


def config_digest(config_path: Path) -> str:
    """Trust digest for a devcontainer config. Trust grants bind to this.

    Covers the whole ``.devcontainer/`` tree — a referenced Dockerfile, compose
    file, or lifecycle script can change what a build executes while
    devcontainer.json stays byte-identical. Build inputs are additionally
    required to stay inside that tree (see assert_build_inputs_contained), so
    the digest covers every input the CLI consumes rather than only the ones
    that happen to live there.

    Blocking I/O. Callers on the event loop must offload it — see the
    ``asyncio.to_thread`` sites in DevcontainerManager and
    resolve_for_work_dir.
    """
    entries = _read_config_tree(config_path)
    is_tree = config_path.parent.name == ".devcontainer"
    cfg_name = config_path.name if is_tree else entries[0][0]
    cfg_bytes = next((b for rel, b in entries if rel == cfg_name), b"")
    _cfg_obj = _parse_jsonc(cfg_bytes)
    assert_build_inputs_contained(_cfg_obj, config_path)
    assert_no_sensitive_host_mounts(_cfg_obj, _project_root_of(config_path), entries)
    return _digest_entries(entries, b"tree" if is_tree else b"file")


# ── Trust store ──────────────────────────────────────────────────────────
#
# JSON file mapping realpath(project_dir) -> {"digest": ..., "granted_at": ...,
# "config_path": ...}. A grant is valid only while the current config bytes
# hash to the recorded digest, so any edit (including by an agent) forces a
# fresh human decision — the devcontainer analogue of Workspace Trust.


def _trust_path() -> Path:
    return config_dir() / "devcontainers" / "trust.json"


def _read_trust() -> dict:
    try:
        data = json.loads(_trust_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@contextmanager
def _locked_trust() -> Iterator[None]:
    """Hold an exclusive lock on the trust store for a whole transaction.

    Grant and revoke are read-modify-write cycles over one JSON object. Without
    a lock spanning the entire cycle, a concurrent revoke of one project and
    grant of another each write back their own stale snapshot, and the later
    write silently resurrects the entry the earlier one removed -- a revoked
    grant surviving is a fail-OPEN outcome, so the lock covers read through
    write rather than just the write.

    Same ``.lock`` sidecar convention as the dependency ledger. Opened ``r+``
    because Windows ``msvcrt.locking`` needs write access on the fd; a
    read-only handle fails EACCES and ``file_lock`` swallows it, which would
    degrade this to a silent no-op.
    """
    path = _trust_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as lf:
        with platform_compat.file_lock(lf.fileno(), exclusive=True):
            yield


def _write_trust(data: dict) -> None:
    """Persist the trust store. Callers must already hold ``_locked_trust()``.

    Writes through ``atomic_write``, which uses ``tempfile.mkstemp`` so
    concurrent writers cannot collide on one temp filename -- a fixed
    ``.tmp`` sibling let two writers interleave into the same path and
    ``os.replace`` a partially written file, or fail outright with ENOENT.
    """
    path = _trust_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def trust_state(project_dir: str | Path) -> tuple[bool, str | None]:
    """``(trusted, digest)`` for the project's CURRENT tree, from ONE walk.

    Both answers come from a single digest computation because the dashboard needs
    them together on a polled endpoint: hashing the tree once for trust and again
    for the digest would double the cost of every poll, and re-deriving trust at
    the call site would put a second copy of the rule beside this one.

    Fails closed. A tree whose digest cannot be computed — one that grew a symlink
    after the grant, or declares features, or exceeds the tree ceilings, all of
    which config_digest refuses — is NOT trusted, and reports no digest rather
    than a partial one. Blocking I/O; callers on the event loop must offload it.
    """
    cfg = find_devcontainer_config(project_dir)
    if cfg is None:
        return (False, None)
    try:
        digest = config_digest(cfg)
    except (OSError, DevcontainerError):
        return (False, None)
    entry = _read_trust().get(os.path.realpath(str(project_dir)))
    trusted = isinstance(entry, dict) and entry.get("digest") == digest
    return (trusted, digest)


def is_trusted(project_dir: str | Path) -> bool:
    """True when the project's CURRENT devcontainer tree carries a grant.

    Thin wrapper over :func:`trust_state` so there is exactly one implementation
    of the rule; see it for the fail-closed contract. Blocking I/O; callers on the
    event loop must offload it.
    """
    return trust_state(project_dir)[0]


def grant_trust(project_dir: str | Path, expected_digest: str | None = None) -> str:
    """Record a trust grant for the project's current config. Returns digest.

    ``expected_digest`` binds the grant to the bytes a human actually
    reviewed: the dashboard passes back the digest it showed in the trust
    prompt, and a mismatch raises instead of granting. Without it there is a
    window between the preview read and the grant in which the agent can
    rewrite ``.devcontainer/`` and have its OWN configuration authorized —
    the digest recorded here is computed from whatever is on disk now, not
    from what was displayed. Optional only so a deliberate caller with no
    prior preview (tests, CLI) can still grant.

    Caller (the dashboard trust endpoint) is responsible for having shown
    the config to a human first; this function only records the decision.
    """
    cfg = find_devcontainer_config(project_dir)
    if cfg is None:
        raise DevcontainerError(f"no devcontainer config under {project_dir}")
    digest = config_digest(cfg)
    if expected_digest is not None and expected_digest != digest:
        raise DevcontainerConfigChanged(
            f"devcontainer config for {project_dir} changed since it was shown: "
            f"reviewed {expected_digest[:12]}, on disk {digest[:12]} — re-read "
            f"the configuration before trusting it"
        )
    key = os.path.realpath(str(project_dir))
    # The read-modify-write runs under one exclusive lock: a concurrent revoke
    # of a different project must not be undone by writing back a snapshot
    # taken before it.
    with _locked_trust():
        data = _read_trust()
        data[key] = {
            "digest": digest,
            "config_path": str(cfg),
            "granted_at": time.time(),
        }
        _write_trust(data)
    logger.info("devcontainer trust granted for %s (digest %s)", key, digest[:12])
    return digest


def revoke_trust(project_dir: str | Path) -> bool:
    """Remove a project's grant. Returns True when one existed.

    Locked across read and write for the same reason as ``grant_trust``, and
    more urgently: losing this update leaves a revoked project still trusted.
    """
    key = os.path.realpath(str(project_dir))
    with _locked_trust():
        data = _read_trust()
        if key not in data:
            return False
        del data[key]
        _write_trust(data)
    logger.info("devcontainer trust revoked for %s", key)
    return True


def config_preview(project_dir: str | Path) -> dict:
    """Digest + raw text of the config, for the dashboard trust prompt.

    The text shown and the digest returned come from ONE read of the tree, so
    they always describe the same bytes. Computing them from two separate walks
    left a window in which the tree could be swapped between them — the card
    would display benign text while the digest (and therefore the grant the
    user's click authorizes) belonged to different content.

    The same symlink / containment / sensitive-path screens that gate the digest
    gate this text, which is returned verbatim to the dashboard caller.
    """
    cfg = find_devcontainer_config(project_dir)
    if cfg is None:
        raise DevcontainerError(f"no devcontainer config under {project_dir}")

    entries = _read_config_tree(cfg)
    is_tree = cfg.parent.name == ".devcontainer"
    cfg_name = cfg.name if is_tree else entries[0][0]
    raw_bytes = next((b for rel, b in entries if rel == cfg_name), b"")
    parsed = _parse_jsonc(raw_bytes)
    assert_build_inputs_contained(parsed, cfg)
    assert_no_sensitive_host_mounts(parsed, _project_root_of(cfg), entries)
    digest = _digest_entries(entries, b"tree" if is_tree else b"file")
    raw = raw_bytes.decode("utf-8", "replace")
    # Files the build would consume beyond devcontainer.json. Surfaced so the
    # prompt can say what else is in scope, not just the json the user reads.
    other_inputs = sorted(rel for rel, _ in entries if rel != cfg_name)
    return {
        "config_path": str(cfg),
        "digest": digest,
        "raw": raw[:65536],
        # Only strings: these render directly in the trust card, and jsonc
        # permits any JSON value here. An object or list would reach React as a
        # child it cannot render, throwing and replacing the chat surface with an
        # error boundary -- an attacker-authored config should not be able to
        # break the very prompt asking whether to trust it. The raw text still
        # shows the real value.
        "name": _preview_str(parsed.get("name")),
        "image": _preview_str(parsed.get("image")),
        "other_inputs": other_inputs[:64],
        "trusted": _digest_matches_grant(project_dir, digest),
    }


#: Largest config the trust prompt will display, and therefore the largest one
#: that can be trusted at all. The digest covers the whole file, so truncating
#: the preview would authorize bytes the reviewer never saw -- the cap is a
#: refusal threshold, not a display convenience.
_MAX_PREVIEW_BYTES = 65536

#: Caps on the rest of the hashed tree. Every sibling is read WHOLLY into memory
#: to be hashed, and the walk is reachable from dashboard status polling, so an
#: oversized file (or a directory full of them) would let a project decide how
#: much gateway memory to consume. A real .devcontainer holds a config, a
#: Dockerfile or compose file, and a few setup scripts, so these are far above
#: any legitimate tree while still bounding the read.
#: Ceiling on the NUMBER of entries in a hashed devcontainer tree. The byte
#: caps below cannot stand in for it: directories are skipped before the byte
#: accounting, so a tree of empty directories is free by that measure while
#: still forcing a Path object per entry into memory. A hand-maintained
#: .devcontainer holds a handful of files; four thousand is already absurd.
_MAX_TREE_ENTRIES = 4096
_MAX_TREE_FILE_BYTES = 2 * 1024 * 1024
_MAX_TREE_TOTAL_BYTES = 16 * 1024 * 1024


def _preview_str(value: object) -> str | None:
    """A displayable string, or None for anything else (including empty)."""
    return value if isinstance(value, str) and value.strip() else None


def _digest_matches_grant(project_dir: str | Path, digest: str) -> bool:
    """True when a recorded grant matches this exact digest.

    Compared against the digest the caller just computed rather than
    re-deriving one, so the preview's ``trusted`` flag cannot disagree with the
    bytes the preview is about.
    """
    key = os.path.realpath(str(project_dir))
    entry = _read_trust().get(key)
    return isinstance(entry, dict) and entry.get("digest") == digest


def _project_token_from_canonical(key: str) -> str:
    """Stable token for an already-realpath'd project key.

    No I/O: callers that already canonicalized off-loop (``up``, ``status``,
    ``_find_by_label``) must use this rather than ``_project_token``, which
    walks the path again and can stall the gateway heartbeat on a
    network-backed mount.
    """
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def _project_token(project_dir: str | Path) -> str:
    """Stable, filesystem-safe identity for one project directory.

    Realpath-keyed so two spellings of the same project agree, and digested so
    the token is short and free of path-charset issues. Shared by the
    container's id-label and the build-artifact layout, which is what makes a
    build directory attributable to a project at all. Blocking; do not call
    from the event loop — use ``_project_token_from_canonical`` on a key that
    was already realpath'd off-loop.
    """
    return _project_token_from_canonical(os.path.realpath(str(project_dir)))


#: Gateway-owned copies projected into a trusted container. Live under ``/tmp``
#: (same AF_UNIX-length reason as the MCP bridge). Never the data home.
AUTH_CONTAINER_DIR = "/tmp/kirocrew-auth"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _gateway_tmp_root() -> Path:
    if sys.platform != "win32" and os.path.isdir("/tmp"):
        return Path("/tmp")
    return Path(tempfile.gettempdir())


def _gateway_tmp(kind: str, project_dir: str | Path) -> Path:
    return _gateway_tmp_root() / f"kirocrew-{kind}" / _project_token(project_dir)


def host_auth_dir(project_dir: str | Path) -> Path:
    return _gateway_tmp("auth", project_dir)


def host_agents_inject_dir(project_dir: str | Path) -> Path:
    return _gateway_tmp("kiro-home", project_dir) / "agents"


def remote_user_home(remote_user: str | None) -> str:
    """Linux-shaped home for ``remoteUser``. ``root`` and empty → ``/root``."""
    if not remote_user or remote_user == "root":
        return "/root"
    if not _SAFE_NAME.match(remote_user):
        return "/root"
    return f"/home/{remote_user}"


def _append_config_list(
    parsed: dict, key: str, entry: object, already: Callable[[object], bool]
) -> None:
    raw = parsed.get(key)
    items: list[Any] = list(raw) if isinstance(raw, list) else []
    if any(already(x) for x in items):
        parsed[key] = items
        return
    items.append(entry)
    parsed[key] = items


def refresh_auth_copy(project_dir: str | Path) -> Path:
    """Copy the host kiro-cli login store into a gateway-owned bind source.

    Live-binding ``~/Library/Application Support`` fails across Docker Desktop
    (SQLite locking + the VM file share). One-way: a login inside the container
    does not write back. Refresh when the host file is newer. ``remoteUser`` is
    a different uid, so the dir is 0755 and the db is 0644.
    """
    dest_root = host_auth_dir(project_dir)
    dest_cli = dest_root / "kiro-cli"
    dest_cli.mkdir(parents=True, exist_ok=True)
    platform_compat.chmod_safe(dest_root, 0o755)
    platform_compat.chmod_safe(dest_cli, 0o755)
    dest_db = dest_cli / "data.sqlite3"
    from kiro_crew.kiro_cli import kiro_cli_state_dbs

    newest: Path | None = None
    newest_mtime = -1.0
    for db in kiro_cli_state_dbs(sys.platform, Path.home(), os.environ):
        try:
            if db.is_file():
                mtime = db.stat().st_mtime
                if mtime > newest_mtime:
                    newest = db
                    newest_mtime = mtime
        except OSError:
            continue
    if newest is None:
        return dest_root
    try:
        if dest_db.is_file() and dest_db.stat().st_mtime >= newest_mtime:
            return dest_root
    except OSError:
        pass
    tmp = dest_db.with_suffix(".tmp")
    shutil.copy2(newest, tmp)
    os.replace(tmp, dest_db)
    platform_compat.chmod_safe(dest_db, 0o644)
    return dest_root


def try_inject_host_agent(
    project_dir: str | Path, agent: str, remote_user: str | None = None
) -> bool:
    """Copy one selected host agent JSON into the bound ``~/.kiro/agents``.

    Never the whole agents directory. Returns True when a file was placed (or
    already matched). ``remote_user`` is unused for the copy itself — the bind
    target was chosen at ``write_build_config`` from the parsed config.
    """
    del remote_user
    if not agent or not _SAFE_NAME.match(agent):
        return False
    from kiro_crew.config.paths import kiro_agents_dir

    src = kiro_agents_dir() / f"{agent}.json"
    try:
        if not src.is_file():
            return False
        data = src.read_bytes()
    except OSError:
        return False
    dest_dir = host_agents_inject_dir(project_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    platform_compat.chmod_safe(dest_dir.parent, 0o755)
    platform_compat.chmod_safe(dest_dir, 0o755)
    dest = dest_dir / f"{agent}.json"
    try:
        if dest.is_file() and dest.read_bytes() == data:
            return True
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        platform_compat.chmod_safe(dest, 0o644)
    except OSError:
        return False
    return True


def inject_auth_mount(parsed: dict, project_dir: str | Path) -> None:
    """Bind the copied login store after the sensitive-path screen."""
    host = str(refresh_auth_copy(project_dir))
    entry = f"source={host},target={AUTH_CONTAINER_DIR},type=bind"
    _append_config_list(parsed, "mounts", entry, lambda m: AUTH_CONTAINER_DIR in str(m))


def inject_agents_mount(parsed: dict, project_dir: str | Path) -> None:
    """Bind a gateway-owned agents dir onto the container user's ``~/.kiro/agents``."""
    dest_dir = host_agents_inject_dir(project_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    platform_compat.chmod_safe(dest_dir.parent, 0o755)
    platform_compat.chmod_safe(dest_dir, 0o755)
    user = parsed.get("remoteUser")
    user_s = user if isinstance(user, str) else None
    if user_s and user_s != "root" and not _SAFE_NAME.match(user_s):
        return
    target = f"{remote_user_home(user_s)}/.kiro/agents"
    host = str(dest_dir)
    entry = f"source={host},target={target},type=bind"
    _append_config_list(parsed, "mounts", entry, lambda m: "/.kiro/agents" in str(m))


def remove_gateway_tmp(kind: str, project_dir: str | Path) -> None:
    """Best-effort teardown of one project's ``/tmp/kirocrew-<kind>/<token>``."""
    root = _gateway_tmp(kind, project_dir)
    try:
        if root.is_symlink():
            root.unlink()
            return
        if not root.is_dir():
            if root.exists():
                root.unlink()
            return
    except OSError:
        return
    try:
        for child in list(root.rglob("*")):
            try:
                if child.is_symlink() or child.is_file():
                    child.unlink()
            except OSError:
                logger.debug("devcontainer: could not reap %s", child, exc_info=True)
        shutil.rmtree(root, ignore_errors=True)
    except OSError:
        logger.debug("devcontainer: could not reap %s", root, exc_info=True)


def remove_runtime_injects(project_dir: str | Path) -> None:
    remove_gateway_tmp("auth", project_dir)
    remove_gateway_tmp("kiro-home", project_dir)


def _build_root(project_dir: str | Path) -> Path:
    """Directory holding one project's sanitized build configs.

    The project component is load-bearing, not cosmetic: a digest-only path
    (``build/<digest>``) cannot be attributed to a project, so superseded
    configs could only be reaped by guessing at unrelated directories. Keying
    the parent by project makes "this project's stale configs" an exactly
    enumerable set.
    """
    return config_dir() / "devcontainers" / "build" / _project_token(project_dir)


# A build directory is named by a digest prefix. Anything else under a project's
# build root was not written by write_build_config, so the reaper leaves it.
_BUILD_DIR_RE = re.compile(r"^[0-9a-f]{24}$")


def _remove_build_entry(entry: Path) -> None:
    """Delete one entry under a project's build root, never following links.

    ``is_symlink`` is tested BEFORE ``is_dir`` because ``is_dir`` follows the
    link: a link planted here would otherwise be treated as a directory and
    ``rmtree`` would delete its target's contents, outside the tree this reaper
    is allowed to touch. A link is unlinked as a link, so only the link dies.
    """
    if entry.is_symlink() or not entry.is_dir():
        entry.unlink()
    else:
        shutil.rmtree(entry)


def _prune_superseded_build_configs(project_dir: str | Path, keep_digest: str) -> None:
    """Reap this project's stale sanitized build configs.

    Without this, every trusted config edit leaves its predecessor's directory
    behind forever. Containment, in order:

    * only ONE project's build root is ever iterated, so another project's
      artifacts are not reachable from here and a whole-tree wipe is not
      expressible;
    * only digest-named directories are candidates, and the digest currently in
      use is always kept;
    * links are never followed (see ``_remove_build_entry``);
    * best-effort — a build must not fail because its cleanup could not.
    """
    root = _build_root(project_dir)
    keep = keep_digest[:24]
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name == keep or not _BUILD_DIR_RE.match(entry.name):
            continue
        try:
            _remove_build_entry(entry)
        except OSError:
            logger.debug("devcontainer: could not reap build config %s", entry, exc_info=True)


def _remove_project_build_configs(project_dir: str | Path) -> None:
    """Reap ALL of one project's build configs (teardown).

    Only the config the next ``up()`` would consume matters, so once a project
    is torn down its whole build root is garbage. Scoped to that one root and
    link-safe for the same reasons as the prune above; best-effort.
    """
    root = _build_root(project_dir)
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if not _BUILD_DIR_RE.match(entry.name):
            continue
        try:
            _remove_build_entry(entry)
        except OSError:
            logger.debug("devcontainer: could not reap build config %s", entry, exc_info=True)
    try:
        root.rmdir()  # only succeeds once empty, so a stray file is preserved
    except OSError:
        pass


def write_build_config(project_dir: str, digest: str) -> Path:
    """Write the sanitized config the build actually consumes. Returns its path.

    Two things this closes:

    * ``initializeCommand`` is the one lifecycle hook the spec runs on the HOST
      ("run on the host machine during initialization"). Honoring it would let
      the project's config execute outside the container entirely, which is the
      one thing the container's existence is supposed to bound. It is stripped
      here, and the build is pointed at this copy via ``--override-config``, so
      the CLI never sees it.
    * The copy is written from the digest-verified bytes and lives under the
      gateway's own keystone-protected dir, so what the CLI parses is what was
      trusted rather than whatever is on disk when the build starts.

    ``--override-config`` relocates ONLY devcontainer.json, so referenced build
    inputs need separate handling, and the two kinds differ in what a mid-build
    swap can actually reach:

    * ``build.dockerfile`` / ``build.context`` still resolve against the
      workspace -- verified by experiment, including with an absolute context --
      so they cannot be relocated. They are instead required to stay inside the
      hashed tree (assert_build_inputs_contained). A swap landing mid-build
      changes only what goes INTO the image, which the agent already controls
      once it has a shell in the container, so the residual is not an escalation.
    * ``dockerComposeFile`` is different in both directions. It resolves against
      the CONFIG FILE's directory rather than the workspace (the CLI's own path
      helper takes ``configFilePath`` as the base, confirmed by a fixture where a
      compose file present ONLY beside the sanitized copy resolved fine), and a
      compose service can request host privilege -- ``privileged``, a bind of
      ``/``, the docker socket. That combination makes it the one referenced
      input worth freezing, and the one that CAN be frozen: the digest-verified
      bytes are copied in beside this config and the reference is rewritten to
      the copy, so a swap during the build is simply not read.
    """
    cfg = find_devcontainer_config(project_dir)
    if cfg is None:
        raise DevcontainerError(f"no devcontainer config under {project_dir}")
    entries = _read_config_tree(cfg)
    is_tree = cfg.parent.name == ".devcontainer"
    cfg_name = cfg.name if is_tree else entries[0][0]
    raw = next((b for rel, b in entries if rel == cfg_name), b"")
    if _digest_entries(entries, b"tree" if is_tree else b"file") != digest:
        raise DevcontainerConfigChanged(
            f"devcontainer inputs for {project_dir} changed after the trust "
            f"check; refusing to build"
        )
    parsed = _parse_jsonc(raw)
    assert_build_inputs_contained(parsed, cfg)
    assert_no_sensitive_host_mounts(parsed, _project_root_of(cfg), entries)
    stripped = parsed.pop(_HOST_LIFECYCLE_KEY, None)
    if stripped is not None:
        logger.warning(
            "devcontainer: ignoring %s for %s — it executes on the host, "
            "outside the container boundary this feature provides",
            _HOST_LIFECYCLE_KEY,
            project_dir,
        )
    out_dir = _build_root(project_dir) / digest[:24]
    out_dir.mkdir(parents=True, exist_ok=True)
    _freeze_compose_files(
        parsed, entries, out_dir, _compose_base_dir(parsed, _project_root_of(cfg))
    )
    _apply_default_resource_caps(parsed)
    # After the sensitive-path screen, same as the DoS ceilings: binds we
    # created, not a project-declared mount of the data home. Do not
    # re-screen: the host paths are gateway-owned under /tmp.
    from kiro_crew.devcontainer_mcp import inject_bridge_mount, inject_host_gateway

    inject_bridge_mount(parsed, project_dir)
    inject_host_gateway(parsed)
    inject_auth_mount(parsed, project_dir)
    inject_agents_mount(parsed, project_dir)
    out = out_dir / "devcontainer.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, out)
    # Reap the predecessors only after the replacement is durable, so a failure
    # above never leaves the project with no usable config at all.
    _prune_superseded_build_configs(project_dir, digest)
    return out


def _is_unlimited_limit(value: str | None) -> bool:
    """True when *value* asks docker for NO ceiling.

    ``--pids-limit=-1`` and ``--memory=0`` are docker's spellings for unlimited.
    Treating them as "the operator set a limit" suppressed the default cap and
    left the container able to exhaust host processes or memory -- the config
    said the opposite of what the presence check concluded.

    A value that cannot be read as a number is left alone: it is either a real
    size (``512m``) or something docker will reject itself, and guessing would
    override a deliberate setting.
    """
    if value is None:
        return False
    raw = value.strip().lower()
    if not raw:
        return True
    try:
        return float(raw) <= 0
    except ValueError:
        return False  # e.g. "512m" -- a real ceiling


def _has_run_flag(run_args: list[str], flag: str) -> bool:
    """Whether ``runArgs`` sets *flag* to an actual ceiling.

    Two ways this used to answer wrongly, in opposite directions:

    * A substring test over the joined argv treats ``--memory-reservation 128m``
      as setting ``--memory`` (it CONTAINS it), so a soft reservation suppressed
      the hard cap. Docker has several such prefixes, so matching is by whole
      flag through the shared tokenizer.
    * A pure presence test treats ``--pids-limit=-1`` as a ceiling when docker
      reads it as unlimited, so the most explicit way to ask for no limit was
      also the way to suppress the default one. An unlimited value now reads as
      ABSENT, which re-injects the configured cap.
    """
    for canon, value in _iter_run_flags(run_args):
        if canon != flag:
            continue
        if _is_unlimited_limit(value):
            continue
        return True
    return False


#: systemd's CPUWeight scale is 1..10000 with 100 as the neutral default; docker
#: expresses the same proportional share as --cpu-shares with 1024 as neutral.
#: Converting rather than copying keeps "half of a fair share" meaning the same
#: thing on both paths, which is what makes the two comparable at all.
_DOCKER_NEUTRAL_CPU_SHARES = 1024
_SYSTEMD_NEUTRAL_CPU_WEIGHT = 100


def _docker_cpu_shares(cpu_weight: int) -> int:
    """Docker ``--cpu-shares`` equivalent of a systemd ``CPUWeight``.

    Clamped to docker's accepted range. A weight that maps below the floor would
    be rejected by the daemon and fail the whole build, which would turn a DoS
    ceiling into an outage.
    """
    shares = round(cpu_weight * _DOCKER_NEUTRAL_CPU_SHARES / _SYSTEMD_NEUTRAL_CPU_WEIGHT)
    return max(2, min(262144, shares))


def _cpu_cap_run_args(run_args: list[str], cpu_weight: int, max_cpu_percent: int) -> list[str]:
    """The CPU flags to append to *run_args*, honoring existing project values.

    Mirrors the host scope: a proportional share always, and a hard quota only
    when the operator opted in (``resource_limits.max_cpu_percent``), because a
    hard cap slows legitimate builds.

    An explicit project value wins, for the same reason it does for memory and
    pids: honoring the repo's config is the point of the feature, and silently
    overriding a deliberate limit would make the container differ from what was
    reviewed at the trust prompt.
    """
    extra: list[str] = []
    if cpu_weight > 0 and not _has_run_flag(run_args, "--cpu-shares"):
        extra += ["--cpu-shares", str(_docker_cpu_shares(cpu_weight))]
    if max_cpu_percent > 0 and not _has_run_flag(run_args, "--cpus"):
        extra += ["--cpus", f"{max_cpu_percent / 100:g}"]
    return extra


def _parse_byte_size(value: str | int | float | None) -> int | None:
    """Bytes for a docker/compose size (``512m``, ``2g``, ``1048576``), or None.

    None means "not a size this can compare" -- the caller then leaves the value
    alone rather than guessing, since replacing an unparseable limit with a cap
    would be a silent behaviour change on a config the reviewer approved.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    raw = str(value).strip().lower()
    if not raw:
        return None
    units = {
        "b": 1,
        "k": 1024,
        "kb": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
        "t": 1024**4,
        "tb": 1024**4,
    }
    for suffix, mult in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if raw.endswith(suffix):
            head = raw[: -len(suffix)].strip()
            try:
                return int(float(head) * mult)
            except ValueError:
                return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _clamped_count(value: str | int | None, ceiling: int) -> int | None:
    """The stricter of *value* and *ceiling*, or None to leave *value* alone.

    Returns None when the value is already at least as strict, so the caller can
    tell "no change needed" from "clamp to this".
    """
    try:
        current = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if current <= 0:
        return ceiling  # unlimited -- the ceiling applies
    return ceiling if current > ceiling else None


def _run_flag_value(run_args: list[str], flag: str) -> str | None:
    """The value ``run_args`` EFFECTIVELY sets for *flag*, or None.

    Returns the LAST occurrence, because that is the one docker honors. Returning
    the first was a real defect: the memory clamp appends `--memory 2048m` after a
    project's `--memory 64g`, and a first-match read then computed the swap ceiling
    from the value docker was about to ignore -- handing back 64g of swap against a
    2048m memory cap.

    Reads through the shared tokenizer so every spelling resolves the same way the
    presence check sees it; two readers with different grammars would disagree
    about what the project actually asked for.
    """
    found: str | None = None
    for canon, value in _iter_run_flags(run_args):
        if canon == flag and value:
            found = value
    return found


def _apply_default_resource_caps(parsed: dict) -> None:
    """Give the container the DoS ceilings the host cgroup scope would have.

    A containerized agent does not go through ``cgroup_scope_argv``, and the
    container's namespaces do not substitute for it: namespaces isolate what the
    process can SEE, they do not cap what it can CONSUME. Without a ceiling a
    fork bomb or an RSS balloon inside the container hits the shared host kernel
    unbounded -- exactly what ``pids.max``/``memory.max`` exist to contain -- and
    a typical devcontainer.json sets no limits of its own.

    The values come from the same config block as the host scope so the two
    paths cannot drift. An explicit project limit is left alone: honoring the
    repo's config is the point of this feature, and silently overriding a
    deliberate limit would make container behavior differ from what the user
    reviewed at the trust prompt.

    Compose services are capped separately, when their file is frozen
    (:func:`_compose_hardened`), because compose ignores ``runArgs``
    and expresses limits in its own schema.

    **RLIMIT is deliberately not mirrored here, and that is not an omission.**
    The host path's only RLIMIT action is ``_raise_nofile``, which RAISES the file
    descriptor soft limit to at least 65536 so the agent does not run out of
    descriptors -- a capability fix, not a ceiling. ``sandbox.py`` states the
    reasoning for the DoS threats directly: ``RLIMIT_NPROC`` is per-real-UID
    rather than per-subtree and ``RLIMIT_AS`` caps virtual rather than resident
    memory, so cgroup ``pids.max``/``memory.max`` are the correct controls -- and
    those ARE mirrored above. Injecting ``--ulimit nofile=65536`` would also risk
    LOWERING the ceiling, since modern images already default far higher and the
    image's value is not knowable when this config is written.
    """
    if parsed.get("dockerComposeFile"):
        return
    from kiro_crew.sandbox import _cgroup_limits_from_config

    max_procs, max_mem_mb, cpu_weight, max_cpu_percent = _cgroup_limits_from_config()
    args = parsed.get("runArgs")
    run_args: list[str] = [str(a) for a in args] if isinstance(args, list) else []
    # A looser project value is clamped rather than honored: this ceiling stands in
    # for the host cgroup scope, and one a config can raise is not a ceiling. A
    # STRICTER project value is still honored -- see the module docstring on
    # _clamped_count.
    pids_now = _run_flag_value(run_args, "--pids-limit")
    if _has_run_flag(run_args, "--pids-limit"):
        clamped = _clamped_count(pids_now, max_procs)
        if clamped is not None:
            run_args += ["--pids-limit", str(clamped)]
    else:
        run_args += ["--pids-limit", str(max_procs)]
    mem_ceiling = max_mem_mb * 1024**2
    mem_now = _parse_byte_size(_run_flag_value(run_args, "--memory"))
    if _has_run_flag(run_args, "--memory") and mem_now and mem_now > mem_ceiling:
        run_args += ["--memory", f"{max_mem_mb}m"]
    elif not _has_run_flag(run_args, "--memory"):
        run_args += ["--memory", f"{max_mem_mb}m"]
    # Swap is settled AFTER the memory branches, independently of which one ran.
    # Tying it to the clamp branch was the bug: a STRICTER project memory limit is
    # honored (by design), so nothing fired, and an explicit `--memory-swap=-1`
    # survived -- 256m of RAM with unlimited swap. --memory-swap equal to --memory
    # denies swap, matching MemorySwapMax=0 on the host path; leaving it looser
    # doubles the effective ceiling no matter how tight --memory is.
    effective_mem = _parse_byte_size(_run_flag_value(run_args, "--memory")) or mem_ceiling
    swap_now = _parse_byte_size(_run_flag_value(run_args, "--memory-swap"))
    swap_declared = _has_run_flag(run_args, "--memory-swap")
    if not swap_declared or swap_now is None or swap_now > effective_mem:
        # swap_now is None for an unlimited spelling (-1/0) as well as for an
        # unparseable one, and both must resolve to the ceiling rather than be
        # left alone -- "unlimited" is the case this whole branch exists for.
        run_args += ["--memory-swap", f"{effective_mem // 1024 ** 2}m"]
    # CPU, which this used to read from config and discard. Namespaces bound what
    # a process can SEE, not what it can consume, so an uncapped container can
    # still starve the host of CPU exactly as it could of memory.
    run_args += _cpu_cap_run_args(run_args, cpu_weight, max_cpu_percent)
    if run_args:
        parsed["runArgs"] = run_args


def _compose_limit_absent(svc: dict, key: str) -> bool:
    """True when *svc* sets no usable ceiling for *key*.

    Absent, blank, or nonpositive all mean "no ceiling": compose passes
    ``pids_limit: -1`` and ``mem_limit: 0`` through to the runtime as unlimited,
    so a presence-only check let the most explicit way to ask for no limit also
    be the way to suppress the injected default.

    Shares :func:`_is_unlimited_limit` with the runArgs path so the two schemas
    cannot drift -- the same hazard has now appeared in both spellings.
    """
    if key not in svc:
        return True
    value = svc.get(key)
    if isinstance(value, bool):  # a YAML `true` is not a ceiling
        return True
    if isinstance(value, (int, float)):
        return value <= 0
    if isinstance(value, str):
        return _is_unlimited_limit(value)
    # Any other shape (list, dict, None) is not a limit compose can honor.
    return True


def _compose_hardened(data: bytes, base_dir: str) -> bytes:
    """Rewrite a compose file for the frozen build copy.

    Two rewrites, and they are related:

    * **Relative host paths become absolute**, resolved against ``base_dir`` --
      the directory the ORIGINAL compose file sits in. Compose resolves relative
      binds against the compose file's own directory, and freezing MOVES the file
      into the build dir, so a relative source would silently re-anchor there:
      ``../../../../.env`` screens harmlessly against ``.devcontainer`` and then
      resolves to the gateway's own data home once frozen. Absolutizing makes the
      screened path and the built path the same string by construction, rather
      than two separate resolutions that have to be kept in agreement. Relative
      binds are how a devcontainer compose normally mounts the project
      (``..:/workspace``), so they are corrected rather than refused.
    * **DoS ceilings are added**, since compose ignores ``runArgs``.
      ``memswap_limit`` is pinned to ``mem_limit`` because otherwise the kernel
      grants swap equal to the cap and the ceiling is effectively doubled.

    A service's own explicit limit wins -- honoring the repo's config is the
    point of the feature, and overriding a deliberate value would make the
    container differ from what was approved at the trust prompt.
    """
    if _yaml is None:
        raise DevcontainerError(
            "a compose-based devcontainer cannot be hardened without a YAML "
            "parser; install pyyaml or use a Dockerfile-based config"
        )

    from kiro_crew.sandbox import _cgroup_limits_from_config  # circular import

    max_procs, max_mem_mb, cpu_weight, max_cpu_percent = _cgroup_limits_from_config()
    try:
        doc = _yaml.safe_load(data.decode("utf-8"))
    except (_yaml.YAMLError, UnicodeDecodeError) as exc:
        # Screening already parsed this file, so a failure here means the bytes
        # are not what was screened. Refuse rather than freeze an unparsed file.
        raise DevcontainerError(f"compose file could not be parsed for hardening: {exc}")
    if not isinstance(doc, dict):
        return data
    services = doc.get("services")
    if not isinstance(services, dict):
        return data

    def _abs(source: str) -> str:
        # A bare name is a named volume with no host side; an already-absolute
        # path needs no re-anchoring.
        if not source or _is_container_absolute(source):
            return source
        if not _looks_like_relative_path(source):
            return source
        return os.path.realpath(os.path.join(base_dir, source))

    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        vols = svc.get("volumes")
        if isinstance(vols, list):
            for i, vol in enumerate(vols):
                if isinstance(vol, dict):
                    src = vol.get("source")
                    if isinstance(src, str):
                        vol["source"] = _abs(src)
                elif isinstance(vol, str):
                    host = _volume_host_part(vol)
                    fixed = _abs(host)
                    if fixed != host:
                        vols[i] = fixed + vol[len(host) :]
        raw_env = svc.get("env_file")
        if isinstance(raw_env, str):
            svc["env_file"] = _abs(raw_env)
        elif isinstance(raw_env, list):
            for i, entry in enumerate(raw_env):
                if isinstance(entry, str):
                    raw_env[i] = _abs(entry)
                elif isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    entry["path"] = _abs(entry["path"])
        # Nonpositive reads as absent on every one of these: compose forwards
        # `-1`/`0` to the runtime as unlimited, so honoring them as "the project
        # chose a ceiling" would let a trusted config opt out of the caps.
        # Absent, unlimited, OR looser than the ceiling all resolve to the cap;
        # a stricter project value is left alone. Same rule as the runArgs path,
        # since compose is only a different spelling of the same request.
        if _compose_limit_absent(svc, "pids_limit"):
            svc["pids_limit"] = max_procs
        else:
            clamped = _clamped_count(svc.get("pids_limit"), max_procs)
            if clamped is not None:
                svc["pids_limit"] = clamped
        mem_ceiling_bytes = max_mem_mb * 1024**2
        if _compose_limit_absent(svc, "mem_limit"):
            svc["mem_limit"] = f"{max_mem_mb}m"
        else:
            current = _parse_byte_size(svc.get("mem_limit"))
            if current and current > mem_ceiling_bytes:
                svc["mem_limit"] = f"{max_mem_mb}m"
        # Swap is handled after mem_limit settles so it can never end up looser
        # than the memory ceiling it is supposed to mirror -- setdefault would
        # also have let an existing `memswap_limit: -1` re-grant unlimited swap.
        swap_now = _parse_byte_size(svc.get("memswap_limit"))
        mem_final = _parse_byte_size(svc.get("mem_limit")) or mem_ceiling_bytes
        if _compose_limit_absent(svc, "memswap_limit") or (swap_now and swap_now > mem_final):
            svc["memswap_limit"] = svc["mem_limit"]
        # Compose spells the CPU ceilings differently from runArgs but means the
        # same thing; without these a compose devcontainer was the one shape with
        # no CPU bound at all.
        if cpu_weight > 0 and _compose_limit_absent(svc, "cpu_shares"):
            svc["cpu_shares"] = _docker_cpu_shares(cpu_weight)
        if max_cpu_percent > 0 and _compose_limit_absent(svc, "cpus"):
            svc["cpus"] = f"{max_cpu_percent / 100:g}"
        build = svc.get("build")
        if isinstance(build, str):
            svc["build"] = _abs(build)
        elif isinstance(build, dict):
            if isinstance(build.get("context"), str):
                build["context"] = _abs(build["context"])
            # additional_contexts needs the same treatment as `context`: adding
            # it to the SCREEN without adding it here would leave a relative
            # value screened against the project but resolved against the build
            # directory once the frozen copy moves there.
            extra = build.get("additional_contexts")
            if isinstance(extra, dict):
                for key, value in list(extra.items()):
                    if isinstance(value, str) and not value.startswith(_NON_PATH_CONTEXT_PREFIXES):
                        extra[key] = _abs(value)
            elif isinstance(extra, list):
                for i, entry in enumerate(extra):
                    if not isinstance(entry, str) or "=" not in entry:
                        continue
                    name, value = entry.split("=", 1)
                    if not value.startswith(_NON_PATH_CONTEXT_PREFIXES):
                        extra[i] = f"{name}={_abs(value)}"

    # The same re-anchoring applies to every host path outside services: the
    # frozen copy sits in the build dir, so a relative one would resolve there
    # rather than where it was screened.
    top_volumes = doc.get("volumes")
    if isinstance(top_volumes, dict):
        for definition in top_volumes.values():
            if not isinstance(definition, dict):
                continue
            opts = definition.get("driver_opts")
            if isinstance(opts, dict) and isinstance(opts.get("device"), str):
                opts["device"] = _abs(opts["device"])
    for section in ("secrets", "configs"):
        entries = doc.get(section)
        if not isinstance(entries, dict):
            continue
        for definition in entries.values():
            if isinstance(definition, dict) and isinstance(definition.get("file"), str):
                definition["file"] = _abs(definition["file"])
    return _yaml.safe_dump(doc, sort_keys=False).encode("utf-8")


def _freeze_compose_files(
    parsed: dict, entries: list[tuple[str, bytes]], out_dir: Path, base_dir: str
) -> None:
    """Copy referenced compose files next to the sanitized config, in place.

    Mutates ``parsed``'s ``dockerComposeFile`` to name the frozen copies. The CLI
    resolves that key against the config file's own directory, so once the copies
    sit beside the sanitized config the live workspace files are never read --
    which is what removes the mid-build swap window for the only referenced input
    that can request host privilege.

    Bytes come from ``entries`` (the digest-verified in-memory tree), never from a
    fresh disk read, so what lands here is what the human approved. A reference
    the tree does not contain is a bug in the containment check rather than
    something to paper over, so it raises instead of silently falling through to
    the live file.
    """
    ref = parsed.get("dockerComposeFile")
    names = [ref] if isinstance(ref, str) else ref
    if not isinstance(names, list) or not names:
        return
    by_rel = dict(entries)
    frozen: list[str] = []
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        rel = name.strip().lstrip("./")
        data = by_rel.get(rel)
        if data is None:
            raise DevcontainerError(
                f"compose file {name!r} is not part of the hashed devcontainer "
                f"tree, so it cannot be frozen for the build"
            )
        # Flatten to a leaf name so the copy always sits beside the config; a
        # nested relpath would resolve outside out_dir and back to live bytes.
        leaf = f"compose-{hashlib.sha256(rel.encode()).hexdigest()[:12]}.yml"
        # Hardened against THIS file's directory, not one shared base: a
        # nested compose file's relative paths resolve against its own dir,
        # so a single base would rewrite them to the wrong host location.
        (out_dir / leaf).write_bytes(_compose_hardened(data, _compose_file_dir(base_dir, name)))
        frozen.append(leaf)
    if frozen:
        parsed["dockerComposeFile"] = frozen if isinstance(ref, list) else frozen[0]


# ── Container lifecycle ──────────────────────────────────────────────────


@dataclass
class DevcontainerInfo:
    """Result of a successful ``devcontainer up`` for one project."""

    container_id: str
    remote_workspace_folder: str
    remote_user: str
    project_dir: str  # realpath key
    config_digest: str
    created_at: float


def _agent_writable(path: str) -> str | None:
    """The first component of *path* this process could write, or None.

    Checking only the file is not enough: a writable PARENT lets the agent
    unlink and recreate the binary, so the whole ancestor chain has to be clean.
    The gateway and the agent run as the same user, so "writable by us" is
    exactly "substitutable by the agent".
    """
    current = os.path.realpath(path)
    if os.path.exists(current) and os.access(current, os.W_OK):
        return current
    while True:
        parent = os.path.dirname(current)
        if os.path.isdir(current) and os.access(current, os.W_OK):
            return current
        if parent == current:
            return None
        current = parent


#: Official Docker Desktop CLI on macOS. The app bundle is user-owned, so
#: :func:`_agent_writable` would refuse it on every ordinary install. Accepted
#: only when Apple Developer ID ``Docker Inc`` still verifies (team
#: ``9BNSXJN65R``). A planted shim at this path fails that check.
_DOCKER_DESKTOP_MAC_CLI = "/Applications/Docker.app/Contents/Resources/bin/docker"
_DOCKER_DESKTOP_TEAM_ID = "9BNSXJN65R"
_docker_desktop_cli_cache: tuple[str, int, int, bool] | None = None


def _signed_docker_desktop_cli(resolved: str) -> bool:
    """True when *resolved* is Docker Desktop's CLI and Developer ID still holds."""
    global _docker_desktop_cli_cache
    if sys.platform != "darwin":
        return False
    try:
        if os.path.realpath(resolved) != os.path.realpath(_DOCKER_DESKTOP_MAC_CLI):
            return False
        st = os.stat(resolved)
    except OSError:
        return False
    cached = _docker_desktop_cli_cache
    if (
        cached is not None
        and cached[0] == resolved
        and cached[1] == st.st_mtime_ns
        and cached[2] == st.st_size
    ):
        return cached[3]
    ok = _codesign_is_docker_inc(resolved)
    _docker_desktop_cli_cache = (resolved, st.st_mtime_ns, st.st_size, ok)
    return ok


def _codesign_is_docker_inc(path: str) -> bool:
    codesign = platform_compat.trusted_system_bin("codesign")
    if codesign is None:
        return False
    try:
        verify = subprocess.run(
            [codesign, "--verify", path],
            capture_output=True,
            timeout=5,
        )
        if verify.returncode != 0:
            return False
        probe = subprocess.run(
            [codesign, "-dv", "--verbose=2", path],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    blob = f"{probe.stdout or ''}{probe.stderr or ''}"
    return f"TeamIdentifier={_DOCKER_DESKTOP_TEAM_ID}" in blob


def _verified_tool(name: str) -> str | None:
    """Absolute path to *name*, or None when it cannot be trusted to run.

    ``shutil.which`` is unusable here. A gateway's PATH routinely leads with
    directories the agent writes -- a worktree venv's ``bin``, ``~/.local/bin``,
    a version-manager shim dir -- so a bare argv name lets the agent plant a
    ``docker`` or ``devcontainer`` shim that the gateway then executes ON THE
    HOST, with the gateway's own environment. That inverts the entire point of a
    feature whose premise is that project code runs inside a container.

    Resolution order: the repo's pinned system directories first, then a PATH hit
    ONLY if neither the binary nor any ancestor directory is writable by this
    process. The second step is what keeps ordinary installs working -- a
    root-owned ``/usr/local/bin/devcontainer`` from ``npm i -g`` is legitimate --
    while still refusing anything the agent could have substituted.

    One exception: Docker Desktop's macOS CLI lives in a user-owned
    ``.app`` bundle. That path is accepted only when Developer ID Docker Inc
    still verifies, so a planted file at the same path is still refused.

    None means "unavailable", and callers degrade to the host path rather than
    running an unverified binary.
    """
    pinned = platform_compat.trusted_system_bin(name)
    if pinned is not None:
        return pinned
    found = shutil.which(name)
    if not found:
        return None
    resolved = os.path.realpath(found)
    if not os.path.isabs(resolved):
        return None
    writable = _agent_writable(resolved)
    if writable is not None:
        if name == "docker" and _signed_docker_desktop_cli(resolved):  # _verified_tool
            return resolved
        logger.warning(
            "devcontainer: refusing to run %s from %s -- %s is writable by this "
            "process, so the binary could have been substituted by agent-run "
            "code. Install it in a root-owned location to enable the feature.",
            name,
            resolved,
            writable,
        )
        return None
    return resolved


def _docker_bin() -> str:
    """Verified ``docker`` path for argv, or refuse.

    Every docker invocation goes through here so a single unverified spawn cannot
    slip in beside the verified ones.
    """
    binary = _verified_tool("docker")
    if binary is None:
        raise DevcontainerError(
            "docker is not available from a trusted location; Dev Containers "
            "cannot run. See the log for the path that was refused."
        )
    return binary


def _cli_argv() -> list[str]:
    """Resolve the devcontainer CLI, which must be an installed binary.

    There is deliberately no ``npx --yes @devcontainers/cli`` fallback. Verifying
    the ``npx`` binary says nothing about the code npx then FETCHES: resolution is
    steered by project-local configuration (``.npmrc`` registry and scope
    settings) that agent-run code can write, so the fallback would download and
    execute an attacker-chosen package ON THE HOST -- outside the container the
    feature exists to confine, and outside anything the trust grant covers.

    An installed binary is a fixed artifact an operator chose, which is what the
    verification in :func:`_verified_tool` can actually reason about.
    """
    binary = _verified_tool("devcontainer")
    if binary:
        return [binary]
    raise DevcontainerError(
        "devcontainer CLI not found in a trusted location: install with "
        "'npm i -g @devcontainers/cli' into a root-owned prefix "
        "(a PATH entry this process can write is refused, and there is no "
        "download-on-demand fallback because fetched code cannot be verified)"
    )


async def _docker_bin_async() -> str:
    """Resolve the docker binary without blocking the event loop.

    ``_docker_bin`` walks PATH stat-ing each candidate AND every ancestor
    directory, so one unresponsive entry (a stale network mount) stalls the whole
    gateway -- not just this feature. Every async caller goes through here; the
    one synchronous caller (``exec_argv``, which builds an argv for a caller that
    is not on the loop) still calls ``_docker_bin`` directly.
    """
    return await asyncio.to_thread(_docker_bin)


def docker_available() -> bool:
    return _verified_tool("docker") is not None


#: Environment switch that admits the Dev Container path at all. Off unless
#: explicitly set, so a normal install cannot enter a container by accident and
#: CI never carries one -- the same shape as the profiler's debug gate.
#:
#: This is deliberately a SECOND lock rather than a replacement for
#: ``agent.devcontainer``. The config key says what the operator wants; this says
#: the operator is a developer who accepted an unfinished feature. Config alone
#: is reachable by anyone following the docs, and the preview still has sharp
#: edges (``python3`` required in the image, host isolation wrappers skipped) --
#: too sharp to hand a user who only flipped a documented setting.
#: Re-exported so callers already importing this module keep one name for the
#: gate. The definition lives in ``constants`` so the dashboard can test it
#: WITHOUT importing this module -- see ``_register_devcontainer_routes``.
DEVCONTAINER_ENV_VAR = _DEVCONTAINER_ENV_VAR

#: Anything outside this reads as off, so a stray ``=0`` means disabled rather
#: than "the name is present, therefore on".
_TRUTHY = ENV_TRUTHY


def dev_optin_enabled(env: dict[str, str] | None = None) -> bool:
    """True when the developer opt-in environment gate is set.

    Read from *env* (defaults to the real environment) so tests do not mutate
    global state.
    """
    source = os.environ if env is None else env
    return source.get(DEVCONTAINER_ENV_VAR, "").strip().lower() in _TRUTHY


def devcontainers_enabled(env: dict[str, str] | None = None) -> bool:
    """True only when BOTH locks are open: the dev opt-in and the config mode.

    Single source of truth for "may this install use Dev Containers at all", so
    the status endpoint the dashboard polls and the spawn-time resolver cannot
    disagree about whether the feature exists.
    """
    if not dev_optin_enabled(env):
        return False
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        return getattr(KiroCrewConfig.load().agent, "devcontainer", "off") == "auto"
    except Exception:
        return False


def gate_refusal_message() -> str:
    """One-line explanation of why the feature is inert, for logs and the CLI."""
    return (
        f"Dev Containers are a developer preview: set {DEVCONTAINER_ENV_VAR}=1 in the "
        f"gateway's environment AND agent.devcontainer=auto in config."
    )


class DevcontainerManager:
    """One container per project directory, built by the devcontainer CLI.

    All state is derivable: the container is found again after a gateway
    restart via its id-label, so nothing here needs persistence. up() calls
    for the same project are serialized (image builds are not concurrent-safe
    on one config).
    """

    def __init__(self) -> None:
        self._infos: dict[str, DevcontainerInfo] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        # Safe without a guard ONLY because there is no await between the
        # get and the set — both run within one event-loop step (N4: this
        # invariant is load-bearing; do not insert awaits here).
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @staticmethod
    def _id_label(key: str) -> str:
        # Stable per-project container identity, sharing the one project-token
        # derivation with the build-artifact layout. *key* is already
        # realpath'd by every caller; hashing it again via _project_token
        # would walk the path on the event loop.
        return f"kirocrew.devcontainer={_project_token_from_canonical(key)}"

    @staticmethod
    async def _discard_container(container_id: str) -> None:
        """Force-remove a container that failed verification. Best effort.

        Used on every path that refuses a freshly built container, so a
        verification failure can never leave one running.
        """
        if not container_id:
            return
        rm = await asyncio.create_subprocess_exec(
            await _docker_bin_async(),
            "rm",
            "-f",
            container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(rm.wait(), timeout=60)
        except asyncio.TimeoutError:
            rm.kill()

    @staticmethod
    def _trusted_digest(project_dir: str, config_path: Path) -> str:
        """The current tree digest, or raise if it is not the granted one.

        Collapses the trust check and the digest read into a single tree read so
        no window exists between "is this trusted" and "what am I building".
        Blocking I/O; callers on the event loop must offload it.
        """
        digest = config_digest(config_path)
        if not _digest_matches_grant(project_dir, digest):
            raise DevcontainerNotTrusted(
                f"devcontainer configuration for {project_dir} is not trusted; "
                f"grant trust in the dashboard before the container can be used"
            )
        return digest

    async def up(self, project_dir: str | Path, *, rebuild: bool = False) -> DevcontainerInfo:
        """Create or reuse the project's devcontainer. Trust-gated.

        Raises DevcontainerNotTrusted before running anything when the
        current config has no valid grant.
        """
        # Off-loop: realpath walks and resolves every component, which blocks on
        # a network-backed or stalled mount. The config reads below are already
        # offloaded for this reason; this one looks like string work and is not.
        key = await asyncio.to_thread(os.path.realpath, str(project_dir))
        cfg = await asyncio.to_thread(find_devcontainer_config, key)
        if cfg is None:
            raise DevcontainerError(f"no devcontainer config under {key}")
        # ONE digest, bound to the grant. Checking is_trusted() and then
        # recomputing the digest separately reads the tree twice: a swap
        # landing between the two reads yields an attacker digest that is
        # internally self-consistent, so write_build_config's re-check passes
        # and unapproved configuration builds. Computing it once and requiring
        # it to equal the recorded grant makes the digest carried downstream
        # the one the human actually approved.
        #
        # Blocking I/O (tree walk + reads), so it runs off the event loop: a
        # large tree would otherwise stall every gateway task while status
        # polling recomputes the hash.
        digest = await asyncio.to_thread(self._trusted_digest, key, cfg)

        async with self._lock_for(key):
            # The pre-lock check is fail-fast. A revoke (or a tree swap) that
            # lands while this call waited for the per-project lock must still
            # refuse: otherwise a session that passed the grant check, queued
            # behind a build, and woke up after trust was withdrawn would
            # still receive the container.
            digest = await asyncio.to_thread(self._trusted_digest, key, cfg)
            cached = self._infos.get(key)
            if cached and cached.config_digest == digest and not rebuild:
                if await self._alive(cached.container_id):
                    return cached
                self._infos.pop(key, None)

            build_config = await asyncio.to_thread(write_build_config, key, digest)
            # Resolved off-loop: _cli_argv walks PATH stat-ing candidates and
            # every ancestor directory, which blocks on a stalled PATH entry.
            cli_argv = await asyncio.to_thread(_cli_argv)
            argv = [
                *cli_argv,
                "up",
                "--workspace-folder",
                key,
                # Build from the sanitized, digest-verified copy rather than the
                # live file, so a host-executing initializeCommand is never seen
                # by the CLI and the parsed config is the trusted one.
                "--override-config",
                str(build_config),
                "--id-label",
                self._id_label(key),
                "--log-format",
                "json",
            ]
            if rebuild or (cached and cached.config_digest != digest):
                argv.append("--remove-existing-container")

            logger.info("devcontainer up starting for %s", key)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=key,
                # Scrubbed, not inherited. The CLI resolves ${localEnv:VAR} from
                # ITS environment, so an inherited gateway env would let a
                # trusted config name a channel credential and have it baked
                # into the image or handed to the container. The container's own
                # namespaces do nothing about this: it is the build's env, not
                # the agent's, and the two are separate surfaces.
                env=_scrubbed_build_env(),
            )

            # Bounded rather than `communicate()`: that buffers each pipe in
            # full for the whole 15-minute budget, so a chatty lifecycle command
            # grows the gateway's heap until it is OOM-killed. Both streams are
            # drained CONCURRENTLY -- draining them in sequence lets the
            # unread one fill its OS buffer and block the child, which converts
            # the memory failure into a hang.
            async def _drain() -> tuple[tuple[bytes, bool], tuple[bytes, bool]]:
                return await asyncio.gather(_read_capped(proc.stdout), _read_capped(proc.stderr))

            try:
                (out_b, out_over), (err_b, err_over) = await asyncio.wait_for(
                    _drain(), timeout=_UP_TIMEOUT_SECS
                )
                await proc.wait()
            except asyncio.TimeoutError:
                proc.kill()
                # Reaped, not just signalled: an un-awaited kill leaves a zombie
                # and an "Unknown child process" warning at interpreter exit.
                await proc.wait()
                raise DevcontainerError(
                    f"devcontainer up timed out after {_UP_TIMEOUT_SECS}s for {key}"
                )
            if out_over or err_over:
                proc.kill()
                await proc.wait()
                raise DevcontainerError(
                    f"devcontainer up for {key} produced more than "
                    f"{_UP_STREAM_LIMIT_BYTES} bytes on one stream and was stopped; "
                    f"quiet the build's lifecycle commands or redirect their output"
                )
            result = self._parse_up_output(out_b.decode(errors="replace"))
            if proc.returncode != 0 or result.get("outcome") != "success":
                tail = err_b.decode(errors="replace")[-2000:]
                desc = result.get("message") or result.get("description") or tail
                raise DevcontainerError(f"devcontainer up failed for {key}: {desc}")

            # Post-build digest re-verification: the devcontainer CLI re-read the
            # config tree from disk during the build, so a swap timed between the
            # pre-check above and the CLI's read would have built UNTRUSTED
            # content. Anything other than a clean match tears the container down
            # rather than handing it to a session.
            container_id = result.get("containerId", "")
            try:
                post_digest = await asyncio.to_thread(config_digest, cfg)
            except Exception as exc:
                # A raise here is not "unknown, carry on": the tree became
                # unreadable, symlinked, or otherwise unverifiable DURING the
                # build, which is exactly when a swap would show up. Discard the
                # container before propagating, or a failed verification would
                # leave a running container nobody vouched for.
                await self._discard_container(container_id)
                raise DevcontainerNotTrusted(
                    f"devcontainer config for {key} could not be re-verified "
                    f"after the build ({exc}); container discarded"
                ) from exc
            if post_digest != digest:
                await self._discard_container(container_id)
                raise DevcontainerNotTrusted(
                    f"devcontainer config for {key} changed during the build; "
                    f"container discarded — re-grant trust for the new config"
                )

            info = DevcontainerInfo(
                container_id=result["containerId"],
                remote_workspace_folder=result.get("remoteWorkspaceFolder", key),
                remote_user=result.get("remoteUser", ""),
                project_dir=key,
                config_digest=digest,
                created_at=time.time(),
            )
            # Preflight: without kiro-cli in the image, the session's later
            # `docker exec ... kiro-cli` exits 127 and surfaces as a generic
            # ACP init failure with no hint of the cause. Fail here with the
            # fix in the message instead.
            #
            # Probed as the SAME user the real exec uses. Without -u, docker runs
            # as the image's default user, so an image where kiro-cli is on root's
            # PATH but not the remoteUser's would pass this probe and then fail
            # 127 at startup -- a preflight that clears the exact case it exists
            # to catch.
            probe_argv = [await _docker_bin_async(), "exec"]
            if info.remote_user:
                probe_argv += ["-u", info.remote_user]
            probe_argv += [info.container_id, "sh", "-c", "command -v kiro-cli"]
            probe = await asyncio.create_subprocess_exec(
                *probe_argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(probe.wait(), timeout=_EXEC_PROBE_TIMEOUT_SECS)
            except asyncio.TimeoutError:
                probe.kill()
                raise DevcontainerError(f"devcontainer for {key} is unresponsive to exec probes")
            if probe.returncode != 0:
                raise DevcontainerError(
                    f"kiro-cli is not installed in the devcontainer for {key}. "
                    f"Install it in the image or via postCreateCommand — see "
                    f"docs/devcontainers.md for the install snippet."
                )
            # Digest equality only proves the tree did not change. A revoke
            # leaves the bytes alone, so without this a build that started
            # trusted would still be handed out after the grant was withdrawn.
            if not await asyncio.to_thread(_digest_matches_grant, key, post_digest):
                await self._discard_container(container_id)
                raise DevcontainerNotTrusted(
                    f"devcontainer trust for {key} was withdrawn during the "
                    f"build; container discarded"
                )
            self._infos[key] = info
            logger.info(
                "devcontainer ready for %s: container=%s workspace=%s user=%s",
                key,
                info.container_id[:12],
                info.remote_workspace_folder,
                info.remote_user or "<image default>",
            )
            return info

    @staticmethod
    def _parse_up_output(stdout: str) -> dict:
        """The up result is the last JSON object on stdout carrying `outcome`.

        --log-format json interleaves log records on the same stream, so scan
        from the end for the result record instead of assuming the last line.
        """
        for line in reversed(stdout.strip().splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and "outcome" in obj:
                return obj
        return {}

    async def _alive(self, container_id: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            await _docker_bin_async(),
            "inspect",
            "--format",
            "{{.State.Running}}",
            container_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            # Tail-bounded like the build read: docker's own output is small, but
            # an unbounded read is the same defect shape either way.
            out_b, _over = await asyncio.wait_for(
                _read_capped(proc.stdout), timeout=_EXEC_PROBE_TIMEOUT_SECS
            )
            await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            # Reaped, not just signalled: an un-awaited kill leaves a zombie.
            await proc.wait()
            return False
        return proc.returncode == 0 and out_b.decode().strip() == "true"

    # ── exec plumbing ────────────────────────────────────────────────────

    def exec_argv(
        self,
        info: DevcontainerInfo,
        inner_argv: list[str],
        *,
        env: dict[str, str],
        exec_id: str,
        workdir: str | None = None,
    ) -> list[str]:
        """Wrap ``inner_argv`` in a ``docker exec`` into the container.

        The inner command runs under ``setsid`` when available so the whole
        in-container tree is one process group that kill_exec() can signal;
        its pid is recorded in a pidfile named by ``exec_id``. Env vars are
        forwarded explicitly with -e (docker exec does not inherit).
        """
        argv = [_docker_bin(), "exec", "-i"]
        if info.remote_user:
            argv += ["-u", info.remote_user]
        argv += ["-w", workdir or info.remote_workspace_folder]
        fwd = dict(env)
        fwd[DEVCONTAINER_EXEC_ENV] = exec_id
        for k, v in fwd.items():
            argv += ["-e", f"{k}={v}"]
        argv.append(info.container_id)
        pidfile = f"{_EXEC_PIDFILE_DIR}/{exec_id}.pid"
        # sh -c preamble: record the pid, prefer setsid for group kill, exec
        # so the recorded pid IS the target (no wrapper shell left behind).
        script = (
            f"mkdir -p {_EXEC_PIDFILE_DIR} && echo $$ > {pidfile}; "
            f'if command -v setsid >/dev/null 2>&1; then exec setsid "$@"; '
            f'else exec "$@"; fi'
        )
        argv += ["sh", "-c", script, "sh", *inner_argv]
        return argv

    async def kill_exec(self, info: DevcontainerInfo, exec_id: str) -> None:
        """Terminate an exec'd process tree inside the container.

        Killing the host-side ``docker exec`` client only detaches; the
        in-container process keeps running. Root discovery order:

        1. Scan /proc/<pid>/environ for the exec marker. A process cannot
           rewrite its OWN environ block, so a marked process cannot shed the
           marker — but this says nothing about what it spawns, see below.
        2. Fallback: the pidfile written by exec_argv's preamble, accepted
           only when strictly numeric, not PID 1, and no leading zero —
           a tampered value like ``1`` would otherwise turn the group kill
           into ``kill -1`` (signal-everything).

        The roots are then expanded TRANSITIVELY over /proc PPID links, because
        neither discovery path reaches a descendant on its own: a child exec'd
        with a scrubbed environment (``env -i``) carries no marker, and ``setsid``
        puts it outside the process group the kill targets. Parentage survives
        both — ``setsid`` does not change it and neither does clearing the
        environment — so the parent link is what actually bounds the set.

        Residual, stated because it cannot be closed here: a descendant that
        double-forks and orphans itself to PID 1 while dropping the marker has no
        remaining local link, and is missed. An unforgeable per-exec boundary
        would need a cgroup or a PID namespace, and ``docker exec`` can create
        neither without the privileges this feature refuses. Sweeping the whole
        container instead would reach it, but a container can serve more than one
        session and killing a sibling's agent is a worse failure than a survivor.

        exec_id is a uuid4 hex generated by the gateway (never
        caller-supplied), so embedding it in the script is injection-safe.
        """
        pidfile = f"{_EXEC_PIDFILE_DIR}/{exec_id}.pid"
        script = (
            f'PIDS=""; '
            f"for E in /proc/[0-9]*/environ; do "
            f'  if tr "\\0" "\\n" < "$E" 2>/dev/null | '
            f'     grep -qx "{DEVCONTAINER_EXEC_ENV}={exec_id}"; then '
            f'    PIDS="$PIDS ${{E#/proc/}}"; '
            f"  fi; "
            f"done; "
            f'PIDS=$(echo "$PIDS" | sed "s|/environ||g"); '
            f'if [ -z "$PIDS" ]; then '
            f"  P=$(cat {pidfile} 2>/dev/null); "
            f'  case "$P" in ""|*[!0-9]*|0*|1) exit 0;; esac; '
            f"  PIDS=$P; "
            f"fi; "
            # Expand to descendants before killing anything. /proc/<pid>/stat is
            # "pid (comm) state ppid ...", and comm can itself contain spaces and
            # parentheses, so the prefix is stripped up to the LAST ") " rather
            # than by field position -- a process named "sh (x)" would otherwise
            # yield a garbage ppid and silently drop its subtree from the set.
            # The outer loop is bounded: it re-scans until nothing new appears, at
            # most as deep as the process tree, so a fork bomb cannot spin it.
            f"for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do "
            f'  ADDED=""; '
            f"  for S in /proc/[0-9]*/stat; do "
            f"    Q=${{S#/proc/}}; Q=${{Q%/stat}}; "
            f'    [ "$Q" = "1" ] && continue; '
            f'    PP=$(sed "s/.*) //" "$S" 2>/dev/null | cut -d" " -f2); '
            f'    [ -z "$PP" ] && continue; '
            f'    case " $PIDS $ADDED " in *" $Q "*) continue;; esac; '
            f'    case " $PIDS $ADDED " in *" $PP "*) ADDED="$ADDED $Q";; esac; '
            f"  done; "
            f'  [ -z "$ADDED" ] && break; '
            f'  PIDS="$PIDS $ADDED"; '
            f"done; "
            # PID 1 is never a target: killing container init tears down the whole
            # container, including any other session's exec sharing it.
            f'PIDS=$(for P in $PIDS; do [ "$P" = "1" ] || echo "$P"; done); '
            f"for P in $PIDS; do "
            f'  kill -TERM -"$P" 2>/dev/null || kill -TERM "$P" 2>/dev/null; '
            f"done; "
            f"sleep 2; "
            f"for P in $PIDS; do "
            f'  kill -KILL -"$P" 2>/dev/null || kill -KILL "$P" 2>/dev/null; '
            f"done; "
            f"rm -f {pidfile}"
        )
        proc = await asyncio.create_subprocess_exec(
            await _docker_bin_async(),
            "exec",
            info.container_id,
            "sh",
            "-c",
            script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=_EXEC_PROBE_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            proc.kill()

    async def _find_by_label(self, key: str) -> str | None:
        """Locate the project's container by id-label (survives restarts)."""
        proc = await asyncio.create_subprocess_exec(
            await _docker_bin_async(),
            "ps",
            "-q",
            "--filter",
            f"label={self._id_label(key)}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            # Tail-bounded like the build read: docker's own output is small, but
            # an unbounded read is the same defect shape either way.
            out_b, _over = await asyncio.wait_for(
                _read_capped(proc.stdout), timeout=_EXEC_PROBE_TIMEOUT_SECS
            )
            await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            # Reaped, not just signalled: an un-awaited kill leaves a zombie.
            await proc.wait()
            return None
        cid = out_b.decode().strip().splitlines()
        return cid[0] if cid else None

    async def status(self, project_dir: str | Path) -> dict:
        """Dashboard-facing status for one project directory.

        ``enabled`` reflects both gates (dev opt-in plus config mode). The
        frontend must not show the trust prompt for a feature that will not run:
        a security prompt with no effect teaches the user to click through
        prompts that do have one. Container lookup falls back to the id-label so
        a live container is still reported after a gateway restart.
        """
        # Off-loop: realpath walks and resolves every component, which blocks on
        # a network-backed or stalled mount. The config reads below are already
        # offloaded for this reason; this one looks like string work and is not.
        key = await asyncio.to_thread(os.path.realpath, str(project_dir))
        cfg = await asyncio.to_thread(find_devcontainer_config, key)
        # Both locks, via the shared helper: with the dev opt-in unset the
        # dashboard must not offer a trust prompt at all, even for a project that
        # ships a config and an operator who flipped agent.devcontainer.
        # Off-loop: reads config from disk, which can block on a
        # network-backed home. Matches the to_thread calls beside it.
        enabled = await asyncio.to_thread(devcontainers_enabled)
        # is_trusted() walks + hashes the tree — off-loop (this endpoint is
        # polled by the dashboard).
        # One call, one walk: the digest is published so the dashboard can bind its
        # dismissal to the config's CONTENT. Keyed on the path alone, a dismissal
        # survived an edit that dropped trust, which is precisely when the prompt
        # has to reappear.
        trusted, cfg_digest = await asyncio.to_thread(trust_state, key) if cfg else (False, None)
        out: dict = {
            "project_dir": key,
            "enabled": enabled,
            "has_config": cfg is not None,
            "config_path": str(cfg) if cfg else None,
            "trusted": trusted,
            "config_digest": cfg_digest,
            "container_id": None,
            "running": False,
            "remote_workspace_folder": None,
        }
        # Every container probe below shells out to the docker binary, so a host
        # that has a devcontainer config but no docker would raise
        # FileNotFoundError straight out of a polled status endpoint. Absent
        # docker there is no container to report, so the lookup is skipped and
        # the config/trust fields — which need no docker — still answer.
        info = self._infos.get(key)
        # Off-loop: walks PATH stat-ing candidates.
        if await asyncio.to_thread(docker_available):
            if info:
                out["container_id"] = info.container_id
                out["running"] = await self._alive(info.container_id)
                out["remote_workspace_folder"] = info.remote_workspace_folder
            elif cfg is not None:
                cid = await self._find_by_label(key)
                if cid:
                    out["container_id"] = cid
                    out["running"] = True
        return out

    async def down(self, project_dir: str | Path) -> bool:
        """Stop and remove the project's container. Returns True if removed.

        Resolves by id-label when the in-memory cache is cold (gateway
        restarted since up()), so a container never becomes unreapable.
        """
        # Off-loop: realpath walks and resolves every component, which blocks on
        # a network-backed or stalled mount. The config reads below are already
        # offloaded for this reason; this one looks like string work and is not.
        key = await asyncio.to_thread(os.path.realpath, str(project_dir))
        info = self._infos.pop(key, None)
        container_id = info.container_id if info else await self._find_by_label(key)
        # The sanitized config is read only while up() builds, so once this
        # project is torn down nothing will consume it again. Reaped even when no
        # container was found, because that is exactly the case that would
        # otherwise leave the artifacts with no later teardown to collect them.
        # Off-loop: the walk and the unlinks are blocking I/O.
        await asyncio.to_thread(_remove_project_build_configs, key)
        from kiro_crew.devcontainer_mcp import remove_bridge_dir

        await asyncio.to_thread(remove_bridge_dir, key)
        await asyncio.to_thread(remove_runtime_injects, key)
        if not container_id:
            return False
        proc = await asyncio.create_subprocess_exec(
            await _docker_bin_async(),
            "rm",
            "-f",
            container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            return False
        return proc.returncode == 0


# Module singleton, mirroring other gateway-wide managers.
_manager: DevcontainerManager | None = None


def get_manager() -> DevcontainerManager:
    global _manager
    if _manager is None:
        _manager = DevcontainerManager()
    return _manager


# ── ACP spawn integration ────────────────────────────────────────────────
#
# TWO spawn paths run a kiro-cli inside a project's container, and both are
# live: AcpRuntime.spawn() backs every chat/subagent session, while
# AcpClient._spawn() backs direct long-lived clients (the Knowledge Library
# worker pool constructs one per worker on the default kiro backend) as well as
# the dormant claude seam. The trust gate, the exec-id mint and the in-container
# kill live here, with both paths as callers, so a change to any of them cannot
# land on one path and silently miss the other.


#: Stable tokens naming why a session with a devcontainer config nonetheless
#: runs on the host. The dashboard maps these to plain language, so they are a
#: published vocabulary: rename one and the UI silently falls back to generic
#: wording.
HOST_REASON_UNTRUSTED = "untrusted"
HOST_REASON_BUILD_FAILED = "build_failed"
HOST_REASON_DOCKER_UNAVAILABLE = "docker_unavailable"
HOST_REASON_CONFIG_CHANGED = "config_changed"
HOST_REASON_UNSUPPORTED_PLATFORM = "unsupported_platform"
#: An enterprise policy floor (``sandbox.min_level``) is in force. The floor
#: names a HOST sandbox tier and is implemented inside ``wrap_argv``, which a
#: containerized spawn skips -- so rather than assert the container satisfies
#: a tier nobody has mapped it to, the session runs on the host where the
#: floor is actually enforced.
HOST_REASON_SANDBOX_FLOOR = "sandbox_floor"


@dataclass(frozen=True)
class ExecutionLocus:
    """Where a session's agent process actually runs, and why.

    ``resolve_for_work_dir`` collapses every negative case to ``None``, which is
    the right answer for the SPAWN (run on the host, as if the feature were
    absent) but loses the distinction a user needs afterwards: having granted
    trust, they believe their commands run inside the project's container, and a
    transient failure that quietly puts them back on their own filesystem is
    indistinguishable from success without this.

    ``mode`` is ``"host"`` only when a config EXISTS and was not used. A work dir
    with no devcontainer config yields no locus at all -- there is no second
    world to have landed in, and reporting one would invent a distinction the
    project does not have.
    """

    mode: str
    container_name: str | None = None
    reason: str | None = None

    def as_payload(self) -> dict[str, str | None]:
        return {
            "mode": self.mode,
            "container_name": self.container_name,
            "reason": self.reason,
        }


#: Last resolved execution locus per work dir. The dashboard reads this to
#: report where a session landed; it is deliberately NOT recomputed there,
#: because a second resolve would probe docker again on a UI request and could
#: disagree with what the session actually did.
_LOCUS_BY_WORK_DIR: dict[str, ExecutionLocus] = {}
_LOCUS_LOCK = threading.Lock()


def record_execution_locus(work_dir: str | Path, locus: ExecutionLocus | None) -> None:
    """Remember (or clear) where work in ``work_dir`` last executed."""
    key = str(work_dir)
    with _LOCUS_LOCK:
        if locus is None:
            _LOCUS_BY_WORK_DIR.pop(key, None)
        else:
            _LOCUS_BY_WORK_DIR[key] = locus


def execution_locus_for(work_dir: str | Path | None) -> ExecutionLocus | None:
    """The locus recorded for ``work_dir``, or None if nothing was recorded."""
    if not work_dir:
        return None
    with _LOCUS_LOCK:
        return _LOCUS_BY_WORK_DIR.get(str(work_dir))


def governed_sandbox_floor() -> str | None:
    """The enterprise ``sandbox.min_level`` floor, or None when ungoverned.

    Delegates to ``sandbox``'s own reader rather than re-resolving the policy, so
    the container gate and ``wrap_argv``'s clamp can never disagree about whether
    a host is governed.

    A ``PlatformCompositionError`` is allowed to propagate exactly as it does for
    ``wrap_argv``: on a host that was supposed to be governed but could not
    compose its policy, reading "no floor" would silently downgrade the very
    control being consulted. The caller treats a raise as "do not containerize".
    """
    from kiro_crew.sandbox import _governance_sandbox_floor

    return _governance_sandbox_floor()


async def resolve_for_work_dir(work_dir: str | Path) -> DevcontainerInfo | None:
    """Resolve the devcontainer for ``work_dir``, or None to run on the host.

    Thin wrapper over :func:`resolve_with_locus` for callers that only need to
    know whether to containerize. See that function for the reasoning; the locus
    exists so the outcome is still reportable after the fact.
    """
    info, _locus = await resolve_with_locus(work_dir)
    return info


async def _resolve_with_locus_inner(
    work_dir: str | Path,
) -> tuple[DevcontainerInfo | None, ExecutionLocus | None]:
    """Resolve the devcontainer, and report where execution actually landed.

    ``info`` is None whenever the session runs on the host: the config mode is
    not ``auto``, the work dir carries no devcontainer config, a config is
    present but has no trust grant, docker is missing, or the build failed. A
    missing grant never blocks the spawn waiting on a human -- the dashboard
    raises the trust prompt out of band -- which matches VS Code: no trust, no
    container.

    ``locus`` is the same outcome in reportable form, and is None only when
    there is nothing to report: no config, or the feature switched off. Logging
    alone made a host fallback explainable to whoever reads the gateway log,
    which is not the person who granted the trust.
    """
    # Off-loop: config read on the session-start path.
    if not await asyncio.to_thread(devcontainers_enabled):
        return None, None
    work = str(work_dir)
    # Both of these walk + hash the .devcontainer tree and this runs on the
    # session-start hot path, so they stay off the event loop.
    if await asyncio.to_thread(find_devcontainer_config, work) is None:
        return None, None
    # An enterprise sandbox floor outranks this feature. The floor is enforced
    # inside wrap_argv, which a containerized spawn skips, so containerizing here
    # would leave a policy the operator set silently inert -- worse than not
    # containerizing, because the dashboard would report the session as confined.
    # Off-loop: resolving the policy can walk profile files.
    try:
        floor = await asyncio.to_thread(governed_sandbox_floor)
    except Exception:
        # Includes PlatformCompositionError: a host that could not compose its
        # policy is treated as governed, never as ungoverned.
        logger.warning(
            "devcontainer: sandbox policy for %s could not be resolved; running on "
            "the host, where the sandbox floor is enforced",
            work,
        )
        return None, ExecutionLocus("host", reason=HOST_REASON_SANDBOX_FLOOR)
    if floor:
        logger.warning(
            "devcontainer requested for %s but sandbox.min_level=%s is in force; "
            "running on the host, where that floor is enforced. The container path "
            "skips wrap_argv, which is what implements the floor.",
            work,
            floor,
        )
        return None, ExecutionLocus("host", reason=HOST_REASON_SANDBOX_FLOOR)
    # Off-loop: PATH walk on the session-start path.
    if not await asyncio.to_thread(docker_available):
        logger.warning(
            "devcontainer requested for %s but docker is not on PATH; running on the host",
            work,
        )
        return None, ExecutionLocus("host", reason=HOST_REASON_DOCKER_UNAVAILABLE)
    if not await asyncio.to_thread(is_trusted, work):
        logger.warning(
            "devcontainer config for %s is not trusted; running on the host "
            "until trust is granted in the dashboard",
            work,
        )
        return None, ExecutionLocus("host", reason=HOST_REASON_UNTRUSTED)
    try:
        info = await get_manager().up(work)
    except DevcontainerNotTrusted:
        # A config edit raced between is_trusted() and up().
        return None, ExecutionLocus("host", reason=HOST_REASON_CONFIG_CHANGED)
    except Exception:
        logger.exception("devcontainer up failed for %s; running on the host", work)
        return None, ExecutionLocus("host", reason=HOST_REASON_BUILD_FAILED)
    return info, ExecutionLocus("container", container_name=info.container_id or None)


async def resolve_with_locus(
    work_dir: str | Path,
) -> tuple[DevcontainerInfo | None, ExecutionLocus | None]:
    """Resolve, and record the outcome so the dashboard can report it.

    The recording happens here rather than at each call site so a new caller
    cannot resolve without the verdict being observable.
    """
    info, locus = await _resolve_with_locus_inner(work_dir)
    record_execution_locus(work_dir, locus)
    return info, locus


@dataclass
class ContainerizedSpawn:
    """An argv to launch, plus the state its owner must retain to kill it."""

    argv: list[str]
    info: DevcontainerInfo
    exec_id: str


async def _agent_definition_probe(info: DevcontainerInfo, agent: str) -> int:
    """Return the probe's exit status. Raises on a hung exec."""
    quoted = shlex.quote(f"{agent}.json")
    script = f"test -f .kiro/agents/{quoted} || test -f ~/.kiro/agents/{quoted}"
    argv = [await _docker_bin_async(), "exec"]
    if info.remote_user:
        argv += ["-u", info.remote_user]
    argv += ["-w", info.remote_workspace_folder, info.container_id, "sh", "-c", script]
    probe = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(probe.wait(), timeout=_EXEC_PROBE_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        probe.kill()
        raise DevcontainerError(
            f"devcontainer for {info.project_dir} is unresponsive to exec probes"
        )
    return int(probe.returncode or 0)


async def ensure_agent_definition_available(info: DevcontainerInfo, agent: str) -> None:
    """Verify the container can resolve ``--agent <agent>``, or refuse.

    Moving kiro-cli into the container moves it away from the Kiro home state it
    needs. Agent definitions are looked up as FILES, and kiro-cli resolves
    ``--agent`` against ``$PWD/.kiro/agents`` before ``~/.kiro/agents``, so the two
    locations differ sharply once containerized:

    * a **project-scoped** definition (``<project>/.kiro/agents/<name>.json``) sits
      inside the bind-mounted workspace, so the container sees it and it works
      unchanged;
    * a **global** one (``~/.kiro/agents/<name>.json``) is host-only. The whole
      host agents directory is never mounted (those files carry MCP credentials
      in ``env``). Instead, when the probe misses, the selected host file is
      copied into a gateway-owned bind on ``~/.kiro/agents`` and the probe
      runs again.

    Fail only when the host also has no file. A silent fallback to a host
    spawn would leave the operator believing the session is containerized.
    """
    if not agent:
        return
    # The name reaches a shell, so it is quoted rather than interpolated -- it
    # comes from configuration, which agent-run code can propose edits to.
    if await _agent_definition_probe(info, agent) == 0:
        return
    injected = await asyncio.to_thread(
        try_inject_host_agent, info.project_dir, agent, info.remote_user
    )
    if injected and await _agent_definition_probe(info, agent) == 0:
        return
    raise DevcontainerError(
        f"agent {agent!r} has no definition inside the devcontainer and none "
        f"could be copied from the host, so kiro-cli cannot start with it. "
        f"Add .kiro/agents/{agent}.json to the project — it is inside the "
        f"mounted workspace — or install {agent}.json under ~/.kiro/agents on "
        f"the host; see docs/devcontainers.md. The host's ~/.kiro/agents is "
        f"not mounted wholesale because those definitions can carry MCP "
        f"credentials."
    )


def containerize_spawn(
    info: DevcontainerInfo,
    inner_argv: list[str],
    *,
    env: dict[str, str] | None = None,
) -> ContainerizedSpawn:
    """Wrap ``inner_argv`` in a docker exec into ``info``'s container.

    The exec id is minted here from uuid4 rather than accepted from a caller:
    ``kill_exec`` interpolates it unquoted into a shell script, so the whole
    injection-safety argument rests on it being gateway-generated hex, and a
    caller-supplied id would move that guarantee out of this module.

    The spawn marker is always forwarded, so the orphan sweep can still
    positively identify the in-container tree as ours.
    """
    exec_id = uuid.uuid4().hex
    fwd = dict(env or {})
    fwd[KIROCREW_SPAWNED_ENV] = KIROCREW_SPAWNED_VALUE
    # Host spawn injects the API key on the process env AFTER this function
    # builds docker-exec ``-e`` pairs, so the container would never see it.
    inject_kiro_cli_api_key(fwd)
    fwd.setdefault("XDG_DATA_HOME", AUTH_CONTAINER_DIR)
    refresh_auth_copy(info.project_dir)
    argv = get_manager().exec_argv(info, inner_argv, env=fwd, exec_id=exec_id)
    return ContainerizedSpawn(argv=argv, info=info, exec_id=exec_id)


async def kill_containerized_tree(info: DevcontainerInfo | None, exec_id: str | None) -> None:
    """Signal the in-container process tree of a containerized spawn.

    A no-op for a host spawn (no info, or no exec id), so a teardown path can
    call it unconditionally. Killing the host-side ``docker exec`` client only
    detaches it while the in-container tree keeps running, so callers must run
    this BEFORE their host-side teardown; a failure here is swallowed because
    aborting on it — e.g. for a container that is already gone — would strand
    the host process that teardown still has to reap.
    """
    if info is None or not exec_id:
        return
    try:
        await get_manager().kill_exec(info, exec_id)
    except Exception:
        logger.warning("devcontainer kill_exec failed for exec %s", exec_id, exc_info=True)
