"""Dev Container support: run a session's kiro-cli inside the project's
devcontainer (VS Code parity).

When ``agent.devcontainer`` is ``"auto"`` and a session's work dir carries a
``.devcontainer/devcontainer.json`` (or ``.devcontainer.json``), the ACP spawn
path replaces the host kiro-cli argv with a ``docker exec`` into a container
built by the reference ``@devcontainers/cli`` — the same engine VS Code uses.
The repo's devcontainer.json is honored in full (image/build, features,
lifecycle hooks, mounts, runArgs) after a one-time per-config human trust
grant, mirroring VS Code's Workspace Trust model. The gateway does NOT strip
or override the file: parity, not a sandbox.

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

Known v1 limitations (documented in docs/devcontainers.md):
  - Kiro Crew's own managed MCP servers (mcp-core/cron/computer) are not
    reachable from inside the container (their REST callback targets the
    gateway's host loopback). kiro-cli reports mcp_server_init_failure and
    the session continues with the project toolchain fully functional.
  - /proc-based liveness observes the host-side ``docker exec`` client
    proxy: death detection works (pipe close), wedge heuristics degrade.
  - Linux hosts only. On macOS, Docker Desktop is a VM; the existing
    Seatbelt sandbox path is unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE

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
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            return fh.read()
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
    for p in sorted(parent.rglob("*")):
        if p.is_symlink():
            raise DevcontainerError(
                f"devcontainer tree contains a symlink, which cannot be "
                f"content-bound to a trust grant: {p}"
            )
        if p.is_dir():
            continue
        # Size-check by stat BEFORE reading. Every member is read wholly into
        # memory to be hashed, and this walk is reachable from dashboard status
        # polling, so without a bound the project decides how much gateway
        # memory to consume.
        try:
            size = p.stat().st_size
        except OSError as exc:
            raise DevcontainerError(f"cannot stat devcontainer input {p}: {exc}") from exc
        if size > _MAX_TREE_FILE_BYTES:
            raise DevcontainerError(
                f"devcontainer input {p.name} is {size} bytes, over the "
                f"{_MAX_TREE_FILE_BYTES}-byte per-file limit for a hashed tree"
            )
        total += size
        if total > _MAX_TREE_TOTAL_BYTES:
            raise DevcontainerError(
                f"the .devcontainer tree exceeds the {_MAX_TREE_TOTAL_BYTES}-byte "
                f"total limit; it cannot be hashed for a trust grant"
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
        entries.append((rel, _read_config_bytes(p, _project_root_of(config_path))))
    return entries


def _digest_entries(entries: list[tuple[str, bytes]], marker: bytes) -> str:
    """Hash an in-memory input set. ``marker`` separates the two layouts so a
    tree and a single file can never collide."""
    h = hashlib.sha256()
    for rel, data in entries:
        h.update(rel.encode())
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
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


def _collect_compose_host_binds(cfg: dict, entries: list[tuple[str, bytes]] | None) -> list[str]:
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
    try:
        import yaml as _yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        # Refuse rather than skip: without a parser the binds cannot be
        # enumerated, and a compose file whose host access is unknown must not
        # be built. Imported lazily and guarded because pyyaml is optional here,
        # matching the other yaml call sites in this package.
        raise DevcontainerError(
            f"a compose-based devcontainer cannot be screened without a YAML "
            f"parser ({exc}); install pyyaml or use a Dockerfile-based config"
        ) from exc

    found: list[str] = []
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
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
                found += _compose_service_host_paths(svc)
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
        found += _compose_top_level_host_paths(doc)
    return [f for f in found if f]


def _compose_service_host_paths(svc: dict) -> list[str]:
    """Host paths one compose SERVICE reaches, across every spelling."""
    found: list[str] = []
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
    return found


def _compose_top_level_host_paths(doc: dict) -> list[str]:
    """Host paths declared outside any service definition."""
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


#: Host interfaces that hand over control of the container runtime or the kernel
#: view of the machine. Binding any of these is an ESCAPE, not a read: with the
#: docker socket the agent can ask the host daemon for a fresh container mounting
#: anything, which walks around every path restriction. They are not
#: credential paths, so ``is_sensitive_path`` does not cover them.
_HOST_CONTROL_PATHS = (
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/var/run/podman/podman.sock",
    "/run/podman/podman.sock",
    "/run/containerd/containerd.sock",
    "/var/run/containerd/containerd.sock",
    "/run/crio/crio.sock",
)

#: Pseudo-filesystems whose whole point is host-wide visibility and control.
_HOST_CONTROL_TREES = ("/proc", "/sys", "/dev")


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

    Matches the socket exactly and the pseudo-filesystems by containment, since
    binding ``/proc`` or a subtree of it is the same class of grant.
    """
    norm = _canonical_for_control_match(resolved)
    if norm in (_canonical_for_control_match(p) for p in _HOST_CONTROL_PATHS):
        return True
    for tree in _HOST_CONTROL_TREES:
        if norm == tree or norm.startswith(tree + "/"):
            return True
    return False


def _looks_like_relative_path(source: str) -> bool:
    """Distinguish a relative PATH from a docker named volume.

    A named volume is a bare token (``myvol``) with no separator; anything
    carrying ``.`` or a separator is a path compose will resolve against the
    compose file's directory, and therefore something that can escape upward.
    """
    return source.startswith((".", "/", "\\")) or "/" in source or "\\" in source


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
    sources += _collect_compose_host_binds(cfg, entries)
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

    raw_mounts = cfg.get("mounts")
    for entry in raw_mounts if isinstance(raw_mounts, list) else [raw_mounts]:
        add_mount(entry)
    add_mount(cfg.get("workspaceMount"))

    args = cfg.get("runArgs")
    if isinstance(args, list):
        flags = [a for a in args if isinstance(a, str)]
        for i, arg in enumerate(flags):
            # -v/--volume take "host:container"; --mount takes the kv form.
            if arg in ("-v", "--volume") and i + 1 < len(flags):
                found.append(_volume_host_part(flags[i + 1]))
            elif arg.startswith(("--volume=", "-v=")):
                found.append(_volume_host_part(arg.split("=", 1)[1]))
            elif arg == "--mount" and i + 1 < len(flags):
                add_mount(flags[i + 1])
            elif arg.startswith("--mount="):
                add_mount(arg.split("=", 1)[1])
            # These read a host file WITHOUT mounting it, so a screen that only
            # understood bind syntax let them through: --env-file pointing at the
            # gateway's own .env hands the container every credential in it, and
            # kiro-cli inside then inherits them. The path is the payload here
            # even though nothing is bound.
            elif arg in _HOST_FILE_READING_FLAGS and i + 1 < len(flags):
                found.append(flags[i + 1].strip())
            else:
                for flag in _HOST_FILE_READING_FLAGS:
                    if arg.startswith(flag + "="):
                        found.append(arg.split("=", 1)[1].strip())
                        break
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


def _scrubbed_build_env() -> dict[str, str]:
    """The environment the devcontainer CLI is allowed to see.

    Imported lazily because ``sandbox`` reaches back into this package's config
    layer; the same reason ``is_sensitive_path`` is imported at its call site.
    """
    from kiro_crew.sandbox import scrub_agent_denied_env

    return scrub_agent_denied_env(dict(os.environ))


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


def is_trusted(project_dir: str | Path) -> bool:
    """True when the project's CURRENT devcontainer tree carries a grant.

    Fails closed: a tree whose digest cannot be computed — including one that
    grew a symlink after the grant (config_digest refuses those) — is NOT
    trusted. Blocking I/O; callers on the event loop must offload it.
    """
    cfg = find_devcontainer_config(project_dir)
    if cfg is None:
        return False
    key = os.path.realpath(str(project_dir))
    entry = _read_trust().get(key)
    if not isinstance(entry, dict):
        return False
    try:
        return entry.get("digest") == config_digest(cfg)
    except (OSError, DevcontainerError):
        return False


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


def _project_token(project_dir: str | Path) -> str:
    """Stable, filesystem-safe identity for one project directory.

    Realpath-keyed so two spellings of the same project agree, and digested so
    the token is short and free of path-charset issues. Shared by the
    container's id-label and the build-artifact layout, which is what makes a
    build directory attributable to a project at all.
    """
    return hashlib.sha256(os.path.realpath(str(project_dir)).encode()).hexdigest()[:24]


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
    out = out_dir / "devcontainer.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, out)
    # Reap the predecessors only after the replacement is durable, so a failure
    # above never leaves the project with no usable config at all.
    _prune_superseded_build_configs(project_dir, digest)
    return out


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
    """
    if parsed.get("dockerComposeFile"):
        return
    from kiro_crew.sandbox import _cgroup_limits_from_config

    max_procs, max_mem_mb, _weight, _cpu = _cgroup_limits_from_config()
    args = parsed.get("runArgs")
    run_args: list[str] = [str(a) for a in args] if isinstance(args, list) else []
    joined = " ".join(run_args)
    if "--pids-limit" not in joined:
        run_args += ["--pids-limit", str(max_procs)]
    if "--memory" not in joined:
        # --memory-swap equal to --memory denies swap, matching MemorySwapMax=0
        # on the host path; without it the kernel grants swap equal to the cap
        # and the ceiling is effectively doubled.
        run_args += ["--memory", f"{max_mem_mb}m", "--memory-swap", f"{max_mem_mb}m"]
    if run_args:
        parsed["runArgs"] = run_args


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
    import yaml as _yaml  # type: ignore[import-untyped]

    from kiro_crew.sandbox import _cgroup_limits_from_config

    max_procs, max_mem_mb, _weight, _cpu = _cgroup_limits_from_config()
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
        if "pids_limit" not in svc:
            svc["pids_limit"] = max_procs
        if "mem_limit" not in svc:
            svc["mem_limit"] = f"{max_mem_mb}m"
            svc.setdefault("memswap_limit", f"{max_mem_mb}m")
        build = svc.get("build")
        if isinstance(build, str):
            svc["build"] = _abs(build)
        elif isinstance(build, dict) and isinstance(build.get("context"), str):
            build["context"] = _abs(build["context"])

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
        (out_dir / leaf).write_bytes(_compose_hardened(data, base_dir))
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


def _cli_argv() -> list[str]:
    """Resolve the devcontainer CLI. Prefer a real binary; fall back to npx.

    ``npx --yes`` downloads on first use; the docs tell operators to install
    ``@devcontainers/cli`` globally for deterministic startup.
    """
    binary = shutil.which("devcontainer")
    if binary:
        return [binary]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--yes", "@devcontainers/cli"]
    raise DevcontainerError(
        "devcontainer CLI not found: install with 'npm i -g @devcontainers/cli' "
        "(or ensure npx is on PATH)"
    )


def docker_available() -> bool:
    return shutil.which("docker") is not None


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
        # derivation with the build-artifact layout.
        return f"kirocrew.devcontainer={_project_token(key)}"

    @staticmethod
    async def _discard_container(container_id: str) -> None:
        """Force-remove a container that failed verification. Best effort.

        Used on every path that refuses a freshly built container, so a
        verification failure can never leave one running.
        """
        if not container_id:
            return
        rm = await asyncio.create_subprocess_exec(
            "docker",
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
        key = os.path.realpath(str(project_dir))
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
            cached = self._infos.get(key)
            if cached and cached.config_digest == digest and not rebuild:
                if await self._alive(cached.container_id):
                    return cached
                self._infos.pop(key, None)

            build_config = await asyncio.to_thread(write_build_config, key, digest)
            argv = [
                *_cli_argv(),
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
            try:
                out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=_UP_TIMEOUT_SECS)
            except asyncio.TimeoutError:
                proc.kill()
                raise DevcontainerError(
                    f"devcontainer up timed out after {_UP_TIMEOUT_SECS}s for {key}"
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
            probe_argv = ["docker", "exec"]
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
            "docker",
            "inspect",
            "--format",
            "{{.State.Running}}",
            container_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=_EXEC_PROBE_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            proc.kill()
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
        argv = ["docker", "exec", "-i"]
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
        in-container process keeps running. Target discovery order:

        1. AUTHORITATIVE: scan /proc/<pid>/environ for the exec marker.
           The environ block is fixed at exec time — the agent process
           cannot rewrite its own marker — so this cannot be spoofed or
           suppressed from inside (M1 review finding: the pidfile CAN).
        2. Fallback: the pidfile written by exec_argv's preamble, accepted
           only when strictly numeric, not PID 1, and no leading zero —
           a tampered value like ``1`` would otherwise turn the group kill
           into ``kill -1`` (signal-everything).

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
            "docker",
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
            "docker",
            "ps",
            "-q",
            "--filter",
            f"label={self._id_label(key)}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=_EXEC_PROBE_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            proc.kill()
            return None
        cid = out_b.decode().strip().splitlines()
        return cid[0] if cid else None

    async def status(self, project_dir: str | Path) -> dict:
        """Dashboard-facing status for one project directory.

        ``enabled`` reflects the agent.devcontainer config mode: the frontend
        must not show the trust prompt for a feature that will not run (M4
        review finding — a no-effect security prompt trains bad clicks).
        Container lookup falls back to the id-label so a live container is
        still reported after a gateway restart (M5).
        """
        from kiro_crew.config.loader import KiroCrewConfig

        key = os.path.realpath(str(project_dir))
        cfg = await asyncio.to_thread(find_devcontainer_config, key)
        enabled = False
        try:
            enabled = getattr(KiroCrewConfig.load().agent, "devcontainer", "off") == "auto"
        except Exception:
            pass
        # is_trusted() walks + hashes the tree — off-loop (this endpoint is
        # polled by the dashboard).
        trusted = bool(cfg) and await asyncio.to_thread(is_trusted, key)
        out: dict = {
            "project_dir": key,
            "enabled": enabled,
            "has_config": cfg is not None,
            "config_path": str(cfg) if cfg else None,
            "trusted": trusted,
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
        if docker_available():
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
        restarted since up()), so a container never becomes unreapable (M5).
        """
        key = os.path.realpath(str(project_dir))
        info = self._infos.pop(key, None)
        container_id = info.container_id if info else await self._find_by_label(key)
        # The sanitized config is read only while up() builds, so once this
        # project is torn down nothing will consume it again. Reaped even when no
        # container was found, because that is exactly the case that would
        # otherwise leave the artifacts with no later teardown to collect them.
        # Off-loop: the walk and the unlinks are blocking I/O.
        await asyncio.to_thread(_remove_project_build_configs, key)
        if not container_id:
            return False
        proc = await asyncio.create_subprocess_exec(
            "docker",
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
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        cfg = KiroCrewConfig.load()
        if getattr(cfg.agent, "devcontainer", "off") != "auto":
            return None, None
    except Exception:
        return None, None
    work = str(work_dir)
    # Both of these walk + hash the .devcontainer tree and this runs on the
    # session-start hot path, so they stay off the event loop.
    if await asyncio.to_thread(find_devcontainer_config, work) is None:
        return None, None
    # Ordered AFTER the config probe so a project with no devcontainer at all
    # reports nothing rather than an "unsupported platform" the user cannot act
    # on and did not ask about.
    if sys.platform != "linux":
        # Docker Desktop is a VM; the parity path is Linux-only in v1.
        return None, ExecutionLocus("host", reason=HOST_REASON_UNSUPPORTED_PLATFORM)
    if not docker_available():
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
