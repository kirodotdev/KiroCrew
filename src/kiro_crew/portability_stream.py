"""Export-archive building for :mod:`kiro_crew.portability`.

The planning pass (:class:`ExportPlan`, :func:`plan_export`) and the two archive
builders -- :func:`create_export_zip` (in-memory, over a ``BytesIO``) and
:func:`stream_export_zip` (over a byte sink, for the streaming HTTP download) --
live here, split out of ``portability.py`` to keep that file under the file-line
cap. The planning helpers they rely on (:func:`_build_export_zip`,
:func:`_measure_nonsession_footprint`) move with them.

Everything here is re-exported from :mod:`kiro_crew.portability`, so the import
path callers use is unchanged: import ``plan_export``, ``stream_export_zip`` and
``create_export_zip`` from ``kiro_crew.portability`` exactly as before. The lower
-level primitives these functions share with the import side (``_mc_dir``,
``_backup_sqlite``, ``_walk_contained``, the session writer/planner, the manifest
version constants, …) stay in ``portability.py`` and are imported here. ``portability``
imports this module only at its very bottom, after those names are defined, and
nothing imports this module before ``portability`` -- so the dependency runs one
way and the import cycle never closes.
"""

from __future__ import annotations

import io
import json
import os
import socket
import time
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePath
from typing import Any, cast

from kiro_crew import pinned_fs
from kiro_crew import portability as _pf
from kiro_crew.member_memory_backup import hold_stores_for_read
from kiro_crew.memory_stores import MEMORY_STORES_DIR_NAME
from kiro_crew.snapshot import EXPORT_MANIFEST_VERSION, is_product_tree_database

# The helpers and constants these functions share with the import side live in
# ``portability.py`` and are reached through ``_pf`` -- the parent MODULE object,
# not names copied out of it. Reading them as ``_pf.<name>`` at CALL time (never at
# import) does two things at once: it keeps the import one-way, so the cycle
# (``portability`` imports this module at its bottom to re-export it) never closes;
# and it keeps a ``monkeypatch.setattr(portability, "<name>", ...)`` visible to this
# code, exactly as it was when these functions lived in ``portability.py``.

#: The stem-order type the planner hands the writer; the same alias as
#: ``portability._SessionOrder``, repeated here for the annotations below.
_SessionOrder = list[str]

__all__ = [
    "ExportPlan",
    "create_export_zip",
    "plan_export",
    "stream_export_zip",
]


def create_export_zip(
    *,
    include_sessions: bool = False,
    memory_only: bool = False,
    include_agent_templates: bool = False,
) -> tuple[bytes, dict]:
    """Create a zip archive of Kiro Crew state. Returns (zip_bytes, manifest_dict).

    *include_sessions* adds the dashboard chats (`_write_sessions`). It is off by
    default because a transcript holds everything the agent was shown, so an archive
    that carries chats is a different thing to hand around than one that does not;
    the manifest then records ``session_count`` and ``sessions_withheld``. With it
    off the archive is unchanged.

    *memory_only* narrows the archive to the snapshot's ``memory`` component
    (`MEMORY_ONLY_FILES` and `MEMORY_ONLY_TREES`): no config, crons, hooks, skills,
    other workspace files or chats, so memory can be handed to someone else without
    the rest of the install. The manifest records ``components: ["memory"]``. It
    wins over *include_sessions*.

    *include_agent_templates* adds the agent templates the crews name, and the ones
    those name in turn (:func:`bundled_agent_templates`), under ``agents/`` with every
    MCP server ``env`` and ``headers`` value replaced by a placeholder. The manifest
    then lists them in ``agent_templates``. Off by default; with it off the archive is
    unchanged. A memory-only export carries none.
    """
    if memory_only:
        include_sessions = False
        include_agent_templates = False
    mc = _pf._mc_dir()
    ts = _pf.datetime.now(_pf.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"kirocrew-export-{ts}"

    _pf._wal_checkpoint(mc / "memory.db")
    _pf._wal_checkpoint(mc / "memory_index.db")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        manifest = _build_export_zip(
            zf,
            prefix,
            include_sessions=include_sessions,
            memory_only=memory_only,
            include_agent_templates=include_agent_templates,
        )
    return buf.getvalue(), manifest


def _add_sqlite_member(zf: zipfile.ZipFile, src: Path, arcname: str) -> int:
    """Back up the database *src* straight into *zf* as *arcname*; return its size.

    The backup streams into the open member (`_backup_sqlite`), so a large memory
    store is never held whole in RAM. The member carries what ``writestr`` gave it
    before: the time it was written and mode ``0600``. ``force_zip64`` because the
    size is not known when the local header goes out, like `_add_from_fd`.
    """
    info = zipfile.ZipInfo(arcname, date_time=time.localtime(time.time())[:6])
    info.compress_type = zf.compression
    info.external_attr = 0o600 << 16
    with zf.open(info, "w", force_zip64=True) as dest:
        return _pf._backup_sqlite(src, dest)


def _build_export_zip(
    zf: zipfile.ZipFile,
    prefix: str,
    *,
    include_sessions: bool,
    memory_only: bool,
    include_agent_templates: bool,
    session_order: _SessionOrder | None = None,
) -> dict:
    """Write every member into *zf* and return the manifest.

    The single enumeration both the in-memory builder (:func:`create_export_zip`,
    over a ``BytesIO``) and the streaming builder (`stream_export_zip`, over a sink
    that pushes bytes to the HTTP response) share, so the two produce equivalent
    archives. ``prefix`` is the archive's top directory; its timestamp is also the
    download filename's.

    *session_order* is the stem order a streaming export's pre-pass hands the writer
    (:func:`_plan_sessions`); the writer still judges each chat authoritatively.
    ``None`` lets :func:`_write_sessions` discover and sort the stems itself.
    """
    mc = _pf._mc_dir()
    contents_summary: dict = {}
    # Core JSON/text files (none of them is memory)
    core_files: tuple[str, ...] = (
        ()
        if memory_only
        else (
            "config.json",
            *_pf._SETTINGS_DOCUMENTS,
            "hooks.json",
            "crons.json",
            "notifications.jsonl",
            "project_dir",
            "workspace_dir",
        )
    )
    for fname in core_files:
        src = mc / fname
        if src.is_file() and not src.is_symlink():
            zf.write(str(src), f"{prefix}/{fname}")
            contents_summary[fname] = src.stat().st_size

    # The crewmate team list: one document in its own directory. Written by name like
    # the core files above -- the directory holds nothing else that rides (its lock
    # file is this host's runtime state), so the pinned tree walk below is not needed.
    teams_src = mc / "crew-teams" / "teams.json"
    if not memory_only and teams_src.is_file() and not teams_src.is_symlink():
        zf.write(str(teams_src), f"{prefix}/crew-teams/teams.json")
        contents_summary["crew-teams/teams.json"] = teams_src.stat().st_size

    # SQLite databases via backup API
    for db_name in ("memory.db", "memory_index.db"):
        src = mc / db_name
        if src.is_file() and not src.is_symlink():
            contents_summary[db_name] = _add_sqlite_member(zf, src, f"{prefix}/{db_name}")

    # Directory trees: workspace, plan_memory, skills
    #
    # ``mc`` is resolved once, before the walk, and the walk is anchored on the
    # RESOLVED root: ``$KIROCREW_HOME`` may itself legitimately be a link, and
    # ``pin_directory`` refuses a reparse point at a name, so pinning the
    # configured spelling would return an empty archive on such a host.
    mc_real = os.path.realpath(mc)
    dir_counts: dict[str, int] = {}
    # A memory-only export walks only the memory half of the workspace; its
    # files still count as ``workspace_files`` so the import preview reads them.
    trees = _pf.MEMORY_ONLY_TREES if memory_only else ("workspace", "plan_memory", "skills")
    for dirname in trees:
        count = 0
        for rel, fd in _pf._walk_contained(mc_real, PurePath(dirname), _pf._keep_for_export):
            try:
                # ``as_posix()`` for the same reason the filter rebuilds from
                # parts: a zip member name is POSIX-separated by spec.
                # ``ZipInfo`` happens to rewrite ``os.sep`` today, so this is
                # not a fix for a live bug -- it stops the archive name
                # depending on that, next to a filter that must not.
                _pf._add_from_fd(zf, fd, f"{prefix}/{rel.as_posix()}")
            finally:
                os.close(fd)
            count += 1
        top = dirname.split("/", 1)[0]
        dir_counts[top] = dir_counts.get(top, 0) + count
    contents_summary["workspace_files"] = dir_counts.get("workspace", 0)
    if not memory_only:
        contents_summary["plan_memory_files"] = dir_counts.get("plan_memory", 0)
        contents_summary["skill_count"] = dir_counts.get("skills", 0)

    # Named memory stores: the same pinned walk, with the tree's own filter and the
    # fence lifted (see `_open_verified`). A store's databases go through the backup
    # API like the root ones -- the gateway holds every store open under WAL while
    # this runs -- and the filter has already dropped their sidecars.
    stores: set[str] = set()
    with hold_stores_for_read(Path(mc_real) / MEMORY_STORES_DIR_NAME):
        for rel, fd in _pf._walk_contained(
            mc_real, PurePath(MEMORY_STORES_DIR_NAME), _pf._keep_store_for_export, fenced_ok=True
        ):
            try:
                arcname = f"{prefix}/{rel.as_posix()}"
                if is_product_tree_database(rel.as_posix()):
                    real = pinned_fs.fd_real_path(fd)
                    if real is None:
                        continue
                    _add_sqlite_member(zf, Path(real), arcname)
                else:
                    _pf._add_from_fd(zf, fd, arcname)
            finally:
                os.close(fd)
            if len(rel.parts) > 1:
                stores.add(rel.parts[1])
    contents_summary["memory_store_count"] = len(stores)

    bundled_templates: list[str] = []
    if include_agent_templates:
        carried, past_cap = _pf.bundled_agent_templates()
        for stem, spec in carried:
            zf.writestr(
                f"{prefix}/{_pf.AGENT_TEMPLATES_DIR}/{stem}.json", json.dumps(spec, indent=2)
            )
            bundled_templates.append(stem)
        contents_summary["agent_template_count"] = len(bundled_templates)
        if past_cap:
            contents_summary["agent_templates_past_cap"] = past_cap

    # Build the envelope before selecting chats so their timestamp map is
    # budgeted against the same document the importer will read.
    manifest = {
        "version": _pf.MEMORY_EXPORT_MANIFEST_VERSION if memory_only else EXPORT_MANIFEST_VERSION,
        "format": "zip",
        "created_at": _pf.datetime.now(_pf.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hostname": socket.gethostname(),
        "user": os.environ.get("USER", "unknown"),
        "contents": contents_summary,
    }
    if memory_only:
        manifest["components"] = ["memory"]
    if include_agent_templates:
        manifest["agent_templates"] = bundled_templates
    if include_sessions:
        session_mtimes: dict[str, float] = {}
        # The writer judges each chat authoritatively under its lock (privacy +
        # the running import budget). A streaming export passes the pre-pass order
        # so the skipped-size header it already sent is an estimate of this count.
        exported, withheld, skipped = _pf._write_sessions(
            zf, mc_real, prefix, session_mtimes, manifest=manifest, order=session_order
        )
        contents_summary["session_count"] = exported
        contents_summary["sessions_withheld"] = withheld
        contents_summary["sessions_skipped_size"] = skipped
        manifest["session_mtimes"] = session_mtimes
    zf.writestr(f"{prefix}/MANIFEST.json", json.dumps(manifest, indent=2))

    return manifest


def _measure_nonsession_footprint(
    mc_real: str,
    *,
    memory_only: bool,
    include_agent_templates: bool,
) -> tuple[int, int]:
    """Stat-only ``(member_count, uncompressed_bytes)`` for every NON-session member.

    Mirrors exactly what :func:`_build_export_zip` writes before the chats -- same
    files, same filters, same order of inclusion -- so the streaming export can
    budget the chat selection against the real baseline without first compressing
    anything. It is deliberately a SAFE OVER-ESTIMATE: a SQLite member is sized by
    its live file (``st_size`` after the WAL checkpoint), and the backup the writer
    actually produces is vacuum-packed to that size or smaller, so the real archive
    is never larger than this pass assumed. Over-estimating can only make the chat
    selection more conservative, never admit a chat that would breach the import
    cap -- which is the fail-safe direction.

    This walks the trees and stores exactly as the writer does (the pinned,
    reparse-safe `_walk_contained`), so a workspace junction is skipped here too and
    is never resolved.
    """
    mc = _pf._mc_dir()
    members = 0
    nbytes = 0

    core_files: tuple[str, ...] = (
        ()
        if memory_only
        else (
            "config.json",
            *_pf._SETTINGS_DOCUMENTS,
            "hooks.json",
            "crons.json",
            "notifications.jsonl",
            "project_dir",
            "workspace_dir",
        )
    )
    for fname in core_files:
        src = mc / fname
        if src.is_file() and not src.is_symlink():
            members += 1
            nbytes += src.stat().st_size

    teams_src = mc / "crew-teams" / "teams.json"
    if not memory_only and teams_src.is_file() and not teams_src.is_symlink():
        members += 1
        nbytes += teams_src.stat().st_size

    for db_name in ("memory.db", "memory_index.db"):
        src = mc / db_name
        if src.is_file() and not src.is_symlink():
            members += 1
            nbytes += _pf._sqlite_logical_size(src)

    trees = _pf.MEMORY_ONLY_TREES if memory_only else ("workspace", "plan_memory", "skills")
    for dirname in trees:
        for rel, fd in _pf._walk_contained(mc_real, PurePath(dirname), _pf._keep_for_export):
            try:
                members += 1
                nbytes += os.fstat(fd).st_size
            finally:
                os.close(fd)

    with hold_stores_for_read(Path(mc_real) / MEMORY_STORES_DIR_NAME):
        for rel, fd in _pf._walk_contained(
            mc_real, PurePath(MEMORY_STORES_DIR_NAME), _pf._keep_store_for_export, fenced_ok=True
        ):
            try:
                if is_product_tree_database(rel.as_posix()):
                    real = pinned_fs.fd_real_path(fd)
                    if real is None:
                        continue
                    members += 1
                    nbytes += _pf._sqlite_logical_size(Path(real))
                else:
                    members += 1
                    nbytes += os.fstat(fd).st_size
            finally:
                os.close(fd)

    if include_agent_templates and not memory_only:
        carried, _past_cap = _pf.bundled_agent_templates()
        for _stem, spec in carried:
            members += 1
            nbytes += len(json.dumps(spec, indent=2).encode("utf-8"))

    return members, nbytes


class ExportPlan:
    """Everything a streaming export must know BEFORE it sends the first byte.

    Produced by :func:`plan_export` from a stat-only pass. ``prefix`` is the
    archive's top directory and names the download file; ``bundled_templates`` is
    what the unbundled-templates header is computed against; ``session_order`` is
    the stem order the writer attempts, and ``sessions_skipped`` is an estimate of
    what the archive leaves out, carried in the ``X-Kirocrew-Sessions-Skipped-Size``
    header.
    """

    __slots__ = (
        "prefix",
        "created_at",
        "include_sessions",
        "memory_only",
        "include_agent_templates",
        "bundled_templates",
        "agent_templates_past_cap",
        "session_order",
        "sessions_skipped",
    )

    def __init__(
        self,
        *,
        prefix: str,
        created_at: str,
        include_sessions: bool,
        memory_only: bool,
        include_agent_templates: bool,
        bundled_templates: list[str],
        agent_templates_past_cap: int,
        session_order: _SessionOrder | None,
        sessions_skipped: int,
    ) -> None:
        self.prefix = prefix
        self.created_at = created_at
        self.include_sessions = include_sessions
        self.memory_only = memory_only
        self.include_agent_templates = include_agent_templates
        self.bundled_templates = bundled_templates
        self.agent_templates_past_cap = agent_templates_past_cap
        self.session_order = session_order
        self.sessions_skipped = sessions_skipped


def plan_export(
    *,
    include_sessions: bool = False,
    memory_only: bool = False,
    include_agent_templates: bool = False,
) -> ExportPlan:
    """Decide a streaming export's shape with a stat-only pass; no bytes are built.

    Runs the WAL checkpoints the build needs, fixes the archive timestamp/prefix,
    enumerates the agent templates that would be bundled, and -- when chats are
    asked for -- estimates the chat selection (:func:`_plan_sessions`) against a
    stat-only measurement of the non-session members (:func:`_measure_nonsession_footprint`).

    The returned plan lets the handler send the ``Content-Disposition`` filename and
    BOTH advisory headers on ``prepare()`` -- before the first body byte -- and hand
    the stem order to :func:`stream_export_zip`. The writer judges each chat again
    under its lock, so ``sessions_skipped`` is only an estimate of what the archive
    leaves out: the writer can skip MORE (a chat turned private, or grown past the
    cap after this pass) or FEWER (a chat deleted or shrunk between this pass and the
    write), so the header is advisory, not an exact count either way.
    """
    if memory_only:
        include_sessions = False
        include_agent_templates = False
    mc = _pf._mc_dir()
    ts = _pf.datetime.now(_pf.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"kirocrew-export-{ts}"
    created_at = _pf.datetime.now(_pf.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _pf._wal_checkpoint(mc / "memory.db")
    _pf._wal_checkpoint(mc / "memory_index.db")

    mc_real = os.path.realpath(mc)

    bundled_templates: list[str] = []
    agent_templates_past_cap = 0
    if include_agent_templates:
        carried, past_cap = _pf.bundled_agent_templates()
        bundled_templates = [stem for stem, _spec in carried]
        agent_templates_past_cap = past_cap

    session_order: _SessionOrder | None = None
    sessions_skipped = 0
    if include_sessions:
        base_members, base_bytes = _measure_nonsession_footprint(
            mc_real, memory_only=memory_only, include_agent_templates=include_agent_templates
        )
        # Build the same manifest skeleton the writer will, so the chat budget
        # reserves against the document the importer actually reads.
        contents_summary: dict = {}
        if include_agent_templates:
            contents_summary["agent_template_count"] = len(bundled_templates)
            if agent_templates_past_cap:
                contents_summary["agent_templates_past_cap"] = agent_templates_past_cap
        manifest_skeleton = {
            "version": EXPORT_MANIFEST_VERSION,
            "format": "zip",
            "created_at": created_at,
            "hostname": socket.gethostname(),
            "user": os.environ.get("USER", "unknown"),
            "contents": contents_summary,
        }
        if include_agent_templates:
            manifest_skeleton["agent_templates"] = bundled_templates
        session_order, sessions_skipped = _pf._plan_sessions(
            mc_real,
            prefix,
            base_members=base_members,
            base_bytes=base_bytes,
            manifest=manifest_skeleton,
        )

    return ExportPlan(
        prefix=prefix,
        created_at=created_at,
        include_sessions=include_sessions,
        memory_only=memory_only,
        include_agent_templates=include_agent_templates,
        bundled_templates=bundled_templates,
        agent_templates_past_cap=agent_templates_past_cap,
        session_order=session_order,
        sessions_skipped=sessions_skipped,
    )


def stream_export_zip(write: Callable[[bytes], int | None], plan: ExportPlan) -> dict:
    """Build the archive described by *plan*, pushing each chunk to *write*; returns the manifest.

    *write* is any sink that takes bytes -- in the dashboard it hands them to the
    HTTP response, so the first byte leaves as soon as the first zip chunk is ready
    rather than after the whole archive is built. The archive matches the one
    :func:`create_export_zip` builds for the same options; only the transport
    differs. The writer judges each chat under its lock in ``plan.session_order``.
    """

    class _Sink:
        # zipfile needs a writable without seek/tell to pick the streaming
        # (data-descriptor) member format; omitting those methods is what makes it
        # stream rather than seek back to patch each local header.
        def write(self, b: bytes) -> int:
            write(b)
            return len(b)

        def flush(self) -> None:
            pass

    with zipfile.ZipFile(cast(Any, _Sink()), "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        return _build_export_zip(
            zf,
            plan.prefix,
            include_sessions=plan.include_sessions,
            memory_only=plan.memory_only,
            include_agent_templates=plan.include_agent_templates,
            session_order=plan.session_order,
        )
