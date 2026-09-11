"""Install the crew bundle into the paths Kiro Crew actually reads, or refuse.

The crew rides in the image at ``/app/crew-bundle`` (PACKAGING-CONTRACT.md, T3).
The previous design uploaded it to S3 and nothing in the container ever read it,
so ten gates went green while the deployment served a default agent. This module
closes that by construction: the supervisor installs the bundle BEFORE the
backend starts, and refuses to boot unless the named crew is the one installed.

WHERE EACH ENTRY GOES -- verified against the Kiro Crew source at
a Kiro Crew source checkout (0.6.0), not inferred from a plausible
name. ``config_dir()`` and ``data_home()`` resolve to the SAME directory, so a
``<home>/config/`` guess would land two of these where nothing reads them:

* ``agent.json`` -> ``<kiro home>/agents/<crew_name>.json``.
  ``agent_discovery.list_agents`` is THE reader of installed agent specs, keyed by
  the spec's ``name``, and it reads and JSON-parses every ``~/.kiro/agents/*.json``
  on each call. The gateway resolves that directory as ``kiro_home() / "agents"``,
  where ``kiro_home()`` is ``$KIRO_HOME`` or ``~/.kiro``. It is NOT under the data
  home and NOT governed by ``KIROCREW_HOME``: the backend is launched with
  ``KIROCREW_HOME=data_home`` but no ``KIRO_HOME`` (``supervisor/backend.py``),
  so the spec lands under the process HOME. Resolved here the same way rather
  than imported, so this module needs no ``kiro_crew`` install (matching the
  supervisor's other minimal, import-free config reads).

  The gateway's chain reaches that path through a private override-BLIND helper,
  which ``test_host_isolation_floor.py::test_the_ambient_resolver_has_exactly_one_caller``
  keeps to a fixed set of callers. This module is not one of them and must not
  become one, so it is named here by what it computes rather than by its symbol:
  a source-text guard cannot tell a docstring from a call, and it is right not to
  try.

* ``mcp.json`` -> ``<data home>/mcp.json``. The Kiro Crew-scope MCP config is
  ``data_home()/"mcp.json"`` -- the highest-priority source in
  ``mcp_discovery._mcp_sources`` (``mcp_discovery.py:297``,
  ``SCOPE_KIROCREW``) and what ``dashboard/handlers/mcp.py:1620``
  (``_kirocrew_mcp_json`` -> ``data_home()/"mcp.json"``) reads for its
  ``mcpServers`` (``:704``).

* ``skills/`` -> ``<data home>/skills/``. ``skills.py:1422`` ``skills_dir()`` is
  ``config_dir() / SKILLS_DIR_NAME`` and ``SKILLS_DIR_NAME == "skills"``
  (``skills.py:49``); ``config_dir()`` (``config/paths.py:265``) honours
  ``KIROCREW_HOME``, so in the container this is ``<data home>/skills``.

THE DIGEST is recomputed here byte-identically to the producer
(``share-my-crew/build/export/crew_export/bundle.py:78`` ``_bundle_digest``) and
its independent verifier (``build/export/tools/verify_bundle.py:50``
``digest_of``), which agree exactly. A different serialisation would fail every
valid bundle, so ``_content_digest`` is copied verbatim rather than reinvented.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import stat
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .. import common
from ..common import Settings

log = logging.getLogger("container.supervisor")

#: The four entries PACKAGING-CONTRACT.md freezes, with the on-disk kind each
#: must be. ``skills`` is a directory (may be empty but MUST exist); the rest are
#: files (``mcp.json`` may be ``{}`` but MUST exist).
BUNDLE_ENTRIES: tuple[tuple[str, str], ...] = (
    ("manifest.json", "file"),
    ("agent.json", "file"),
    ("mcp.json", "file"),
    ("skills", "dir"),
)

#: Written at the data-home root on a successful install. For a human reading the
#: logs; T4's gate proves the crew from the image digest, not from this file.
INSTALLED_MARKER = ".smc-crew-installed.json"


def default_kiro_agents_dir() -> Path:
    """Where kiro-cli reads agent specs: ``<kiro home>/agents``.

    Mirrors ``kiro_crew.config.paths.kiro_home`` (``config/paths.py:510``):
    ``$KIRO_HOME`` if set, else ``~/.kiro``, then ``/agents``. Deliberately NOT
    under the data home -- see the module docstring. The one behaviour not
    mirrored is ``kiro_home``'s rejection of a system-directory ``$KIRO_HOME``;
    that guards a pathological override the container never sets, and copying it
    would only widen this module's surface.
    """
    override = os.environ.get("KIRO_HOME")
    home = Path(override).expanduser() if override else Path.home() / ".kiro"
    return home / "agents"


def _content_digest(root: Path) -> str:
    """Recompute the bundle content digest, byte-identical to the producer.

    Copied verbatim from ``crew_export/bundle.py:78`` and ``verify_bundle.py:50``
    (which agree): sha256 over sorted ``[rel_posix, sha256(bytes)]`` rows for
    every file except the top-level ``manifest.json`` (it carries the digest),
    then sha256 of the compact-JSON payload, prefixed ``"sha256:"``. The
    ``sorted(root.rglob("*"))`` over ``Path`` objects and the
    ``separators=(",", ":")`` compaction are both load-bearing: change either and
    the digest of a valid bundle stops matching.
    """
    rows: list[list[str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == "manifest.json":
            continue
        rows.append([rel, hashlib.sha256(path.read_bytes()).hexdigest()])
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_json_object(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise common.ConfigError(
            f"bundle check failed [{label} is readable JSON]: {path} could not be "
            f"read as JSON ({exc})."
        ) from exc
    if not isinstance(data, dict):
        raise common.ConfigError(
            f"bundle check failed [{label} is a JSON object]: {path} parsed to a "
            f"{type(data).__name__}, not an object."
        )
    return data


# Bundle install writes into the data home, which under a mounted persistent volume
# (this image's Dockerfile provisions ``/var/lib/kirocrew`` for exactly that) carries over
# from a prior task. A sandboxed agent in that prior task can write ``mcp.json`` and the
# other destinations, so it can plant a SYMLINK there pointing outside the data home.
# ``install_bundle`` then runs at the NEXT boot, before the backend and any sandbox, as the
# image's own user, and a plain ``shutil.copyfile``/``copytree`` FOLLOWS that symlink and
# overwrites the target outside any confinement. So every destination write below refuses a
# symlink AT the destination, the same way the (extracted) backup reader refused one at its
# source: check and write are the same ``O_NOFOLLOW`` open, not a stat-then-write pair a
# swap can slip between.


def _write_nofollow(dst: Path, data: bytes) -> None:
    """Write *data* to *dst*, refusing to follow a symlink planted at *dst*.

    ``O_NOFOLLOW`` fails with ``ELOOP`` when the final component is a symlink, so a
    pre-planted link cannot redirect this write outside the data home. ``O_TRUNC`` gives the
    same whole-file-replace semantics ``copyfile`` had for the ordinary (non-symlink) case.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(dst), flags, 0o644)
    except OSError as exc:
        raise common.ConfigError(
            f"bundle check failed [destination is not a symlink]: {dst} could not be opened "
            f"for writing without following a link ({exc}). A pre-planted symlink there would "
            f"redirect the unsandboxed install to overwrite a file outside the data home, so "
            f"the install refuses rather than follow it."
        ) from exc
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        raise common.ConfigError(
            f"bundle check failed [destination write]: {dst} could not be written ({exc})."
        ) from exc


def _mkdir_or_refuse(dst: Path, *, what: str) -> None:
    """Create *dst* as a directory, or refuse with a message naming the path.

    ``mkdir(exist_ok=True)`` raises ``FileExistsError`` when the path is an existing
    FILE, and an uncaught one here is worse than a refusal in a way that is specific to
    this deployment: the data home may be a persistent volume, so a regular file left
    where a directory belongs makes the supervisor crash, ECS restart the task, and the
    next boot hit the same file. That is a boot loop that spends the owner's money in
    their own account and never says what is wrong.

    So every directory the container creates on that volume goes through here. The
    container does not get to assume anything about what is on the volume, and the
    answer to an assumption that fails is a refusal that names the path -- the same
    answer ``verify_sandbox`` gives.
    """
    try:
        dst.mkdir(parents=True, exist_ok=True)
    except FileExistsError as exc:
        raise common.ConfigError(
            f"{what} cannot be created: {dst} exists and is not a directory. Something "
            f"left it there, and the container refuses to start rather than crash on it "
            f"every time the task restarts. Remove it, or start on a clean data home."
        ) from exc
    except OSError as exc:
        raise common.ConfigError(f"{what} could not be created at {dst} ({exc}).") from exc


def _prepare_dir_nofollow(dst: Path) -> None:
    """Ensure *dst* is a real directory to copy into, refusing a symlink at that path.

    ``copytree(dirs_exist_ok=True)`` into a path that is a symlink to a directory would write
    through the link. ``os.path.islink`` is checked BEFORE ``is_dir`` because a symlink to a
    directory answers True to ``is_dir`` -- the link is what must be refused, not resolved.
    """
    if os.path.islink(str(dst)):
        raise common.ConfigError(
            f"bundle check failed [destination is not a symlink]: {dst} is a symlink. A "
            f"pre-planted link there would redirect the unsandboxed skills install outside "
            f"the data home, so the install refuses rather than copy through it."
        )
    _mkdir_or_refuse(dst, what="a skills directory")


def _clear_link_at(dst: Path) -> None:
    """Remove a symlink sitting where the bundle needs a real file or directory.

    A link at a bundle-managed path is a collision on a path the bundle owns, and the
    link itself is a pointer rather than content: unlinking it destroys no work, and its
    TARGET is never opened, so nothing outside the data home is touched. Refusing
    instead would be the wrong direction here -- a leftover link in the crew's own
    skills tree would then stop every future task from booting, on a volume an operator
    may not be able to reach.

    The skills ROOT is different and is still refused outright (see
    ``_install_skills_tree``): a link there redirects the entire install, and there is
    no path the bundle owns beneath it to reason about.
    """
    if os.path.islink(str(dst)):
        log.info("skills: removing a symlink planted at %s before installing over it", dst.name)
        dst.unlink()


def _copytree_nofollow(src: Path, dst: Path) -> None:
    """Copy the trusted *src* tree into *dst*, refusing a symlink at any destination path.

    ``copytree`` alone would follow a symlink pre-planted at a NESTED destination the same way
    it would at the top level. This walks the trusted source and writes each file through
    ``_write_nofollow`` and each subdirectory through ``_prepare_dir_nofollow``, so no
    destination component -- top-level or nested -- can redirect a write outside the tree.
    The source is in-image and digest-verified, so it is trusted; only the destinations,
    which live on the possibly-persistent data home, are guarded.
    """
    for entry in sorted(src.iterdir()):
        target = dst / entry.name
        if entry.is_dir() and not entry.is_symlink():
            _clear_link_at(target)
            _prepare_dir_nofollow(target)
            _copytree_nofollow(entry, target)
        elif entry.is_file() and not entry.is_symlink():
            _clear_link_at(target)
            _write_nofollow(target, entry.read_bytes())
        # A symlink IN THE SOURCE is skipped: the bundle is laid out as plain files and
        # dirs, so a link there is not something to reproduce into the read path.


def _bundle_skill_files(src: Path) -> list[str]:
    """The bundle's own skill files, as sorted POSIX-relative paths.

    This is the BUNDLE-MANAGED SET: derived from the tree in the image layer rather
    than from a marker anyone could have written, so it needs no trust and cannot be
    forged by the running crew. Files only -- directories are not managed, because a
    directory can hold both a bundle's file and a crew's, and pruning at directory
    granularity is what deletes the second.
    """
    return sorted(
        path.relative_to(src).as_posix()
        for path in src.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def _marker_mac(payload: dict, secret: str) -> str:
    """The authentication tag over a marker payload.

    Computed over the payload WITHOUT its own ``mac`` key, serialised the way the writer
    serialises it (sorted keys, compact separators) so the reader can reproduce the exact
    bytes. A tag over a re-serialisation that differs by a space authenticates nothing.
    """
    body = {key: value for key, value in payload.items() if key != "mac"}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _previously_installed_skills(settings: Settings) -> list[str] | None:
    """The bundle-managed set from the LAST install, or ``None`` when it is not known.

    The marker lives in the data home, which the crew's own model worker can write, so
    what it says is an INPUT rather than a record this code owns. Constraining what the
    list may CONTAIN does not fix that; the premise has to change. So the marker carries
    an authentication tag, and a marker whose tag does not verify is not read.

    The key is ``SMC_CONTROL_SECRET``. It is the one value the supervisor holds that the
    worker provably does not: ``build_backend_env`` removes it from the environment the
    backend is launched with, and the worker inherits that environment, which
    ``test_backend_env_drops_control_secret.py`` pins. Relocating the marker instead was
    the other option GPT named and it is not available here: the image runs the
    supervisor and the worker as the SAME user (``USER crew``), so no directory on the
    persistent volume is writable by one and not the other, and a location in the
    read-only image layer cannot record what a PREVIOUS task installed, which is the
    marker's whole job.

    ``None`` -- meaning "prune nothing" -- is returned for every reason the record cannot
    be trusted or read: no control secret configured (so nothing can be authenticated),
    no tag, a tag that does not verify, unreadable or unparseable content, or a marker
    from a build that did not record the list. Each of those is logged as itself, because
    "the tag is wrong" and "there is no marker" are very different things to be told.
    """
    marker = settings.data_home / INSTALLED_MARKER
    try:
        raw = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("skills: the install marker could not be read (%s); pruning nothing", exc)
        return None
    if not isinstance(raw, dict):
        log.warning("skills: the install marker is not an object; pruning nothing")
        return None

    recorded = raw.get("skill_files")
    if not isinstance(recorded, list):
        return None

    secret = settings.control_secret
    if not secret:
        log.warning(
            "skills: no control secret is configured, so the install marker cannot be "
            "authenticated; pruning nothing"
        )
        return None
    presented = raw.get("mac")
    if not isinstance(presented, str) or not presented:
        log.error(
            "skills: the install marker carries no authentication tag; it did not come "
            "from this container's supervisor. Pruning nothing."
        )
        return None
    if not hmac.compare_digest(presented, _marker_mac(raw, secret)):
        log.error(
            "skills: the install marker's authentication tag does not verify, so its "
            "contents were not written by this container's supervisor. Pruning nothing."
        )
        return None

    return [entry for entry in recorded if isinstance(entry, str)]


def _validated_managed_entries(entries: list[str]) -> list[str] | None:
    """The recorded entries, or ``None`` when the record cannot be trusted at all.

    Every entry must be a plain relative path: not absolute, already normalised, and
    with no ``..``, no empty and no ``.`` component. An entry that is not is REFUSED
    rather than repaired, and it discards the WHOLE record rather than just itself.

    All-or-nothing is the point, and it is a decision rather than a shortcut. This file
    lives in the data home, which the crew can write, so a malformed entry means the
    record is either corrupt or hostile -- and in both cases the rest of the list is no
    more trustworthy than the bad entry. Dropping only the offender would keep pruning
    from a list something else has been editing. Discarding the record falls back to the
    same path an absent marker takes: prune nothing, install over the top, log why. That
    leaves residue an operator can delete instead of deleting from a list that points
    who-knows-where, and it does not fail the boot, because refusing to start over a
    corrupt bookkeeping file would be a worse answer than the residue.

    This is shape only. It does not establish that an entry stays inside the skills
    directory when it is used -- a string with no ``..`` in it still leaves through a
    symlinked parent -- which is why the deletion itself walks descriptors.
    """
    for entry in entries:
        problem: str | None = None
        if not entry:
            problem = "it is empty"
        elif entry != os.path.normpath(entry):
            problem = "it is not normalised"
        elif PurePosixPath(entry).is_absolute() or entry.startswith("/"):
            problem = "it is absolute"
        elif any(part in ("", ".", "..") for part in PurePosixPath(entry).parts):
            problem = "it has a '.', '..' or empty component"
        elif "\\" in entry or "\x00" in entry:
            problem = "it contains a backslash or a NUL"
        if problem is not None:
            log.error(
                "skills: the recorded bundle file list is not trustworthy -- entry %r is "
                "refused because %s. Pruning nothing this install; a stale skill may remain "
                "and can be deleted by hand.",
                entry,
                problem,
            )
            return None
    return entries


def _unlink_within(dst: Path, rel: str) -> str:
    """Delete ``dst/rel``, refusing anything that would act outside *dst*.

    Returns ``"pruned"``, or a short reason the entry was left alone.

    Containment is enforced by CONSTRUCTION, not by comparing strings. Each directory
    component is opened relative to the previous descriptor with ``O_NOFOLLOW |
    O_DIRECTORY``, so a component swapped for a symlink is refused by the kernel at the
    moment it is traversed, and the final ``unlink`` runs relative to the last
    descriptor. There is no window between deciding the path is inside *dst* and acting
    on it, because the path is never re-resolved by name: the directory verified is the
    directory deleted from. ``crew/packaging/build.py`` uses the same shape for a
    different object.

    A resolved-containment check runs first as well. It is redundant against the walk
    but it is what turns "this entry leaves the tree through a link" into its own named
    refusal instead of a generic ``ELOOP``, and the message is the thing an operator
    reads.
    """
    candidate = dst / rel
    try:
        resolved = candidate.resolve()
        root = dst.resolve()
    except OSError as exc:
        return f"its path could not be resolved ({exc})"
    if resolved != root and root not in resolved.parents:
        return f"it resolves to {resolved}, outside the skills directory"

    parts = PurePosixPath(rel).parts
    try:
        fd = os.open(str(dst), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        return f"the skills directory could not be opened ({exc})"
    open_fds = [fd]
    try:
        for part in parts[:-1]:
            try:
                fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=open_fds[-1],
                )
            except OSError as exc:
                return f"the component {part!r} is not a real directory ({exc})"
            open_fds.append(fd)
        leaf = parts[-1]
        try:
            st = os.lstat(leaf, dir_fd=open_fds[-1])
        except FileNotFoundError:
            return "it is already gone"
        except OSError as exc:
            return f"it could not be inspected ({exc})"
        if not stat.S_ISREG(st.st_mode):
            return "it is no longer a plain file"
        try:
            os.unlink(leaf, dir_fd=open_fds[-1])
        except OSError as exc:
            return f"it could not be removed ({exc})"
        return "pruned"
    finally:
        for open_fd in open_fds:
            os.close(open_fd)


def _install_skills_tree(src: Path, dst: Path, *, previous: list[str] | None) -> None:
    """Lay the bundle's skills into *dst*, replacing its own files and nothing else.

    ``install_bundle`` runs at every boot and the data home may be a persistent
    volume, so this has two jobs that pull against each other:

    * **A skill a later bundle DROPPED must not stay active.** A stale,
      possibly governance-relevant skill silently in the read path is the hazard.
    * **A skill the RUNNING CREW created must not be deleted.** A reinstall's job is
      to replace the bundle's own files; a crew's own work is not its to remove.

    Replacing the whole directory satisfies the first and violates the second, so the
    two sets are separated instead. *previous* is the bundle-managed set from the last
    install, recorded in the marker; anything in it that this bundle does not ship is
    pruned, and everything else is left exactly where it is. When *previous* is
    ``None`` the last install's set is unknown, so nothing is pruned: residue beats
    destruction, and the log says which of the two happened.

    Deliberately not an atomic whole-tree swap. Such a swap guards a reader observing a
    partially-installed tree, and there is no such reader: the install runs in ``run()``
    step 0, before the backend process exists. What matters here is that no write
    follows a link, which each individual file write guarantees.
    """
    if os.path.islink(str(dst)):
        raise common.ConfigError(
            f"bundle check failed [destination is not a symlink]: {dst} is a symlink. A "
            f"pre-planted link there would redirect the unsandboxed skills install outside "
            f"the data home, so the install refuses rather than copy through it."
        )
    managed = _bundle_skill_files(src)
    trusted = None if previous is None else _validated_managed_entries(previous)
    if trusted is None:
        log.info(
            "skills: no trustworthy record of what the last install managed; installing "
            "without pruning so nothing the crew created can be removed"
        )
        stale: list[str] = []
    else:
        stale = sorted(set(trusted) - set(managed))

    _prepare_dir_nofollow(dst)
    pruned = 0
    for rel in stale:
        outcome = _unlink_within(dst, rel)
        if outcome == "pruned":
            pruned += 1
        else:
            log.warning("skills: leaving %s because %s", rel, outcome)
    _copytree_nofollow(src, dst)
    _prune_empty_dirs(dst)
    log.info("skills: %d bundle file(s) installed, %d stale pruned", len(managed), pruned)


def _prune_empty_dirs(root: Path) -> None:
    """Remove directories under *root* that are now empty, deepest first.

    Pruning a dropped skill's file leaves its directory behind, and an empty directory
    is nobody's work, so removing it is not the destruction this function's caller
    guards against. A directory that still holds anything -- a crew's file, a file this
    install declined to touch -- is left, which is what keeps that true.
    """
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not path.is_symlink():
            try:
                path.rmdir()
            except OSError:
                pass  # not empty, which is the answer


def install_bundle(settings: Settings, *, agents_dir: Path | None = None) -> dict:
    """Verify the bundle, then lay it out where Kiro Crew reads it. Fail CLOSED.

    Called from ``run()`` before the backend starts, alongside ``verify_layout``
    / ``require_api_key`` / ``verify_sandbox``. Every refusal names the
    check that failed and both values, because a container that boots with the
    wrong crew is the exact failure this change exists to prevent.

    ``agents_dir`` is injected only by tests, so the agent spec never lands in the
    real ``~/.kiro/agents`` during a test run; production resolves it via
    :func:`default_kiro_agents_dir`. Returns the marker payload on success.
    """
    bundle_dir = settings.bundle_dir

    # 1. The bundle dir and each of its four entries must exist, with the right
    #    kind. The crew rides in an image layer, which cannot be absent -- so a
    #    missing one means the image was built wrong, not a runtime blip.
    if not bundle_dir.is_dir():
        raise common.ConfigError(
            f"bundle check failed [bundle dir present]: SMC_BUNDLE_DIR="
            f"{bundle_dir} is not a directory (exists={bundle_dir.exists()}). The "
            f"crew rides in the image at this path; booting without it would "
            f"serve a default agent."
        )
    for entry, kind in BUNDLE_ENTRIES:
        p = bundle_dir / entry
        ok = p.is_dir() if kind == "dir" else p.is_file()
        if not ok:
            raise common.ConfigError(
                f"bundle check failed [entry present]: expected a {kind} at {p}, "
                f"but exists={p.exists()} is_file={p.is_file()} "
                f"is_dir={p.is_dir()}."
            )

    manifest = _read_json_object(bundle_dir / "manifest.json", "manifest.json")
    agent_spec = _read_json_object(bundle_dir / "agent.json", "agent.json")

    # 2. manifest crew_name must equal SMC_CREW_NAME. An empty SMC_CREW_NAME is
    #    refused too: "it started" must mean "the NAMED crew is installed", and
    #    an unnamed crew cannot satisfy that even if the manifest also omits it.
    manifest_crew = str(manifest.get("crew_name") or "")
    if not settings.crew_name:
        raise common.ConfigError(
            "bundle check failed [manifest crew_name == SMC_CREW_NAME]: "
            f"SMC_CREW_NAME is empty (manifest crew_name={manifest_crew!r}). "
            "The deployment must name the crew it intends to serve."
        )
    if manifest_crew != settings.crew_name:
        raise common.ConfigError(
            "bundle check failed [manifest crew_name == SMC_CREW_NAME]: "
            f"manifest crew_name={manifest_crew!r} != SMC_CREW_NAME="
            f"{settings.crew_name!r}. The image does not carry the crew this "
            "task was configured to serve."
        )

    # 3. agent.json name must equal crew_name, or kiro-cli resolves a different
    #    agent (or none) -- surfacing later as a mode error, not the naming bug.
    agent_name = str(agent_spec.get("name") or "")
    if agent_name != manifest_crew:
        raise common.ConfigError(
            "bundle check failed [agent.json name == crew_name]: agent.json "
            f"name={agent_name!r} != manifest crew_name={manifest_crew!r}. The "
            f"spec is installed at <agents>/{manifest_crew}.json and read back by "
            "its own name, so a mismatch serves nothing."
        )

    # 4. The recomputed content digest must equal the manifest's. This is what
    #    proves the bytes in the image are the bytes that were reviewed.
    manifest_digest = str(manifest.get("digest") or "")
    recomputed = _content_digest(bundle_dir)
    if recomputed != manifest_digest:
        raise common.ConfigError(
            "bundle check failed [content digest == manifest digest]: recomputed="
            f"{recomputed!r} != manifest digest={manifest_digest!r}. The bundle "
            "content does not match what the manifest was signed over."
        )

    # All checks passed -- install into the read paths verified above.
    agents = agents_dir if agents_dir is not None else default_kiro_agents_dir()
    _mkdir_or_refuse(agents, what="the kiro-cli agents directory")
    # A crew name is a NAME, checked before it becomes a path segment. Every check above
    # is an EQUALITY or a digest: they prove the manifest agrees with the spec and with the
    # bytes, and none of them constrains the SHAPE of the agreed name. So a manifest and a
    # spec that both say ``../../etc/whatever`` pass all four and then
    # ``agents / f"{manifest_crew}.json"`` resolves outside ``agents``, because
    # ``Path.__truediv__`` treats an absolute segment as a new root and ``..`` as a parent
    # step -- and ``shutil.copyfile`` writes there, before the backend starts, as root in
    # the image.
    #
    # Whether the name can be attacker-chosen depends on how the deploy tooling populates
    # it, and that tooling is in another track. That is the reason to check here rather
    # than the reason not to: this is the process that does the write, so this is where the
    # answer holds regardless of what set the value.
    #
    # The builder has the same guard for the same reason (``_validated_crew_name`` in
    # ``packaging/build.py``). Duplicated rather than shared, like the no-follow opener:
    # this tree is image source the gateway must not import.
    if (
        not manifest_crew
        or manifest_crew in {".", ".."}
        or "/" in manifest_crew
        or "\\" in manifest_crew
        or "\x00" in manifest_crew
        or os.path.isabs(manifest_crew)
    ):
        raise common.ConfigError(
            f"bundle check failed [crew name is a name]: crew_name={manifest_crew!r} "
            "contains a path separator, is absolute, or is a directory reference. The "
            "spec is installed at <agents>/<crew_name>.json, so a name that can leave "
            "that directory would overwrite a file outside it."
        )
    # One guard, not two. A containment assertion on the resolved destination was here as
    # defence in depth and it is unreachable: with the shape check above in place, no name
    # gets far enough to land outside ``agents``, so no test could redden it. A guard no
    # test can fail is a comment claiming a property nobody verifies, so it is gone rather
    # than shipped. If the join ever changes shape, the check to add back is the one that
    # can be tested against the new shape.
    agent_dst = agents / f"{manifest_crew}.json"
    # Copy the validated bytes rather than re-serialising, so what kiro-cli reads
    # is exactly what the digest covered -- through a no-follow open so a symlink
    # pre-planted at the destination cannot redirect the write.
    _write_nofollow(agent_dst, (bundle_dir / "agent.json").read_bytes())

    _mkdir_or_refuse(settings.data_home, what="the data home")
    mcp_dst = settings.data_home / "mcp.json"
    _write_nofollow(mcp_dst, (bundle_dir / "mcp.json").read_bytes())

    skills_dst = settings.data_home / "skills"
    # Read the last install's bundle-managed set BEFORE the marker is rewritten below,
    # or the pruning decision would be made against this install's own answer.
    previous_skills = _previously_installed_skills(settings)
    _install_skills_tree(bundle_dir / "skills", skills_dst, previous=previous_skills)

    payload = {
        "crew_name": manifest_crew,
        "bundle_digest": manifest_digest,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        # The bundle-managed set, so the NEXT install can prune exactly what this one
        # installed and leave everything else. Recorded rather than inferred because
        # the next install sees only its own bundle, which cannot tell it what a
        # previous bundle put on this disk.
        "skill_files": _bundle_skill_files(bundle_dir / "skills"),
    }
    # Authenticated with the one secret the worker's environment does not carry, so the
    # next install can tell this record from one the crew wrote. Without a control secret
    # there is nothing to sign with, and the next install will read the record as
    # untrusted and prune nothing -- which is the safe direction, so the install still
    # proceeds rather than refusing here.
    if settings.control_secret:
        payload["mac"] = _marker_mac(payload, settings.control_secret)
    else:
        log.warning(
            "skills: no control secret is configured, so the install marker cannot be "
            "authenticated; a later install will not prune from it"
        )
    marker = settings.data_home / INSTALLED_MARKER
    _write_nofollow(marker, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
    log.info(
        "crew %r installed: agent=%s mcp=%s skills=%s digest=%s",
        manifest_crew,
        agent_dst,
        mcp_dst,
        skills_dst,
        manifest_digest,
    )
    return payload
