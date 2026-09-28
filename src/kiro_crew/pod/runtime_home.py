"""A pod's isolated home: fixture seeding, its OS home, and reclamation.

Everything here writes into or deletes from a pod home, and does so through
pinned directory descriptors (``pinned_fs``) so a link or name substitution under
the pod root cannot redirect the operation. Windows has no ``dir_fd``; its twins
hold a ``pin_directory`` handle on each level instead and state the narrower
guarantee they give. Seed sanitization, the owner-only directory helper and the
platform flags stay in :mod:`kiro_crew.pod.runtime` and are read from there at
call time, the namespace the pod suite patches.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import sys
import time
import urllib.parse
from pathlib import Path

from kiro_crew import pinned_fs
from kiro_crew import seed as seed_mod
from kiro_crew.atomic_write import atomic_write_at
from kiro_crew.identity_stores import StoreMapping, store_mappings
from kiro_crew.platform_compat import is_link_or_junction, open_file_no_reparse, pin_directory
from kiro_crew.pod import launchd, runtime
from kiro_crew.pod import windows as win_backend
from kiro_crew.pod.config import PodConfig
from kiro_crew.pod.runtime import PodError
from kiro_crew.seed import SeedError


def is_scenario_ref(value: str) -> bool:
    """Return whether a seed value is a fixture name rather than a directory path."""
    if not value or value.startswith(("~", ".")):
        return False
    if "/" in value or "\\" in value or os.sep in value:
        return False
    return True


def resolve_seed_scenario(value: str) -> str:
    """Validate that *value* names a shipped fixture."""
    available = seed_mod.available_fixtures()
    if value in available:
        return value
    listed = ", ".join(available) if available else "(none)"
    raise PodError(
        f"unknown seed scenario {value!r}. Available scenarios: {listed}.\n"
        f"  To seed from a directory instead, pass a path: "
        f"--seed ./{value} or --seed /abs/path/{value}"
    )


def _open_seed_regular_file(home_fd: int, name: str) -> int:
    """Open seed metadata without letting a FIFO block before type validation."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(name, flags, dir_fd=home_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"seeded home entry {name!r} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _prepare_seeded_home_fd(home_fd: int) -> None:
    """Finish pod-owned setup without reopening the seeded home by name."""
    try:
        fd = _open_seed_regular_file(home_fd, "config.json")
    except FileNotFoundError:
        data: dict = {}
    except OSError as exc:
        raise PodError(f"could not open seeded config.json: {exc}") from exc
    else:
        try:
            with os.fdopen(fd, "r", encoding="utf-8", errors="strict") as handle:
                fd = -1
                text = handle.read(1024 * 1024 + 1)
        except (OSError, UnicodeError) as exc:
            raise PodError(f"could not read seeded config.json: {exc}") from exc
        finally:
            if fd >= 0:
                os.close(fd)
        if len(text) > 1024 * 1024:
            raise PodError("seeded config.json exceeds the 1 MiB pod setup limit")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise PodError(f"seeded config.json is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise PodError("seeded config.json must contain a JSON object")

    runtime._apply_seed_config_floor(data)
    atomic_write_at(home_fd, "config.json", json.dumps(data, indent=2), fsync=True, mode=0o600)

    try:
        os.mkdir("workspace", 0o700, dir_fd=home_fd)
    except FileExistsError:
        pass
    workspace_fd = os.open("workspace", pinned_fs.dir_flags(), dir_fd=home_fd)
    os.close(workspace_fd)


def _fixture_name_from_manifest_text(text: str) -> str:
    """Read the one package-owned scalar used as the seed completion marker."""
    for line in text.splitlines():
        if not line.startswith("fixture-name:"):
            continue
        recorded = line.partition(":")[2].strip()
        if len(recorded) >= 2 and recorded[0] == recorded[-1] and recorded[0] in "\"'":
            recorded = recorded[1:-1]
        return recorded
    return ""


def _seeded_scenario_from_fd(home_fd: int) -> str | None:
    """Return the completion marker from a pinned pod-home descriptor."""
    fd = -1
    try:
        fd = _open_seed_regular_file(home_fd, seed_mod.FIXTURE_MANIFEST)
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as handle:
            fd = -1
            return _fixture_name_from_manifest_text(handle.read(64 * 1024))
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def _refuse_reparse_chain(home_dir: Path) -> None:
    """Refuse when any component of *home_dir* or its parents is a reparse point.

    The win32 stand-in for the ``O_NOFOLLOW`` on every component of a pinned walk.
    Windows exposes no ``dir_fd``, so the ancestors cannot be held open one at a
    time; screening each of them for a symlink or a junction is what keeps a
    planted link from redirecting the whole pod home somewhere else. Checked from
    the drive root downward so the outermost swap is the first refusal.
    """
    for component in (*reversed(home_dir.parents), home_dir):
        if pinned_fs.is_reparse_point(component):
            raise PodError(
                f"refusing to seed pod home {home_dir}: {component} is a symbolic link "
                "or a junction, so the path does not name the directory it appears to"
            )


def _seed_home_windows(cfg: PodConfig, name: str, scenario: str, home_dir: Path) -> bool:
    """Populate pod *name* on win32, stating exactly what it does and does not prove.

    **The guarantee, and the ORDER is the guarantee.** The deepest ancestor of the
    home that already exists is screened for a reparse point and PINNED before
    anything is created, and every level below it is created under the pin of the
    level above; the home itself is then created by this process and a handle plus
    an :func:`pinned_fs.fd_real_path` witness is taken on it -- the kernel's own
    name for the inode now held open, which carries no link component left to
    swap. Both paths run under that pin, the fresh seed and the
    already-seeded restart. Each fixture file is copied from a pinned source
    descriptor (``copy_file_pinned``'s ``src_fd`` form, the only pinned source form
    on this platform) into an ``O_CREAT | O_EXCL`` destination under that witnessed
    home, so nothing existing is ever overwritten. The witness is re-read before the
    completion manifest is published, so a home that changed identity mid-seed is
    refused instead of booted, and the manifest stays the last write -- a partial
    tree carries no completion marker and is refused on the next ``up``.

    **The residual, named rather than implied.** The held handle is opened without
    ``FILE_SHARE_DELETE`` (``platform_compat.pin_directory``), so from the moment
    the anchor pin is taken the home and every directory above it can be neither
    renamed nor deleted; an ancestor swap after that point is refused by the
    kernel, not merely detected. Before it, the anchor is still LOCATED by name,
    and no witness closes that -- a witness taken before the pin can only be
    compared against itself. What also stays open is the home's CONTENTS: each
    fixture entry is reached by name under the pinned home, so a process running as
    this same user could plant a reparse point at a not-yet-written child name
    inside that window. ``O_EXCL`` refuses an entry that already exists, which
    covers a planted file or link at a leaf, and the ``_prepare_seeded_home_dir``
    screen covers the two subdirectories the seed creates. Windows offers no
    ``dir_fd`` to close the rest of that window, and a pod home lives under a plane
    root only this user can write, so the residual is exactly the trust domain the
    OS already grants that user -- the same boundary ``pod/README.md`` records for
    pod isolation generally.

    Returns True when this call seeded the home, False when a complete seed for the
    requested scenario is already present -- the same contract as the POSIX branch.
    """
    _refuse_reparse_chain(home_dir)
    fds: list[int] = []
    home_fd = -1
    try:
        try:
            # Pin BEFORE creating, for the reason
            # ``_pin_outermost_existing_windows`` documents: the old order walked
            # the ancestors with ``mkdir(parents=True)`` and took its first pin
            # only at the home itself, so an ancestor swapped in that window had
            # the whole fixture -- and the completion marker that certifies it --
            # published inside the swapped tree, and this function returned True.
            anchor_fd, anchor = _pin_outermost_existing_windows(home_dir, what="the pod home")
            fds.append(anchor_fd)
            target = anchor
            for part in home_dir.relative_to(anchor).parts[:-1]:
                target = target / part
                fds.append(_pin_created_dir_windows(fds[-1], target, what=f"pod root {part}"))
        except OSError as exc:
            raise PodError(f"could not prepare pod root {home_dir.parent}: {exc}") from exc

        # The LAST level is created here rather than through
        # ``_pin_created_dir_windows`` because this branch has to know whether IT
        # created the home: that helper's ``mkdir(exist_ok=True)`` would erase the
        # fresh / already-seeded distinction the restart path below depends on. The
        # screen it would have done is done here instead, and the parent is pinned
        # either way, so the ordering guarantee is unchanged.
        if pinned_fs.is_reparse_point(home_dir):
            raise PodError(
                f"refusing to seed pod home {home_dir}: it is a symbolic link or a "
                "junction, so the path does not name the directory it appears to"
            )
        fresh = True
        try:
            home_dir.mkdir(mode=0o700)
        except FileExistsError:
            fresh = False
        except OSError as exc:
            raise PodError(f"could not create pod home {home_dir}: {exc}") from exc

        # Pinned and witnessed for BOTH paths, including the restart path below.
        # Reading the completion marker and re-preparing the home purely by name
        # would let a pod that has been seeded once be restarted out of a directory
        # swapped in since, which is the one path where nothing at all is held.
        #
        # TRANSLATED in the same operation that takes the pin, exactly as the POSIX
        # branch translates its ``os.open(..., dir_flags())``: ``pin_directory``
        # refuses anything that is not a plain directory -- a stale FILE left at the
        # home name, or a reparse point planted between the screen above and this
        # open -- and it does so with ``NotADirectoryError``. ``boot`` and the
        # scenario call site catch only ``PodError``, so an untranslated one escapes
        # as an UNINSTRUMENTED crash: no FATAL line, no recorded refusal, no terminal
        # exit code. Do NOT pre-screen with ``home_dir.is_dir()`` instead -- that is a
        # by-name stat which FOLLOWS a reparse point, so it passes the adversarial
        # case this open exists to refuse, and it re-opens the check/use window the
        # pin closes.
        try:
            home_fd = pin_directory(home_dir)
        except OSError as exc:
            raise PodError(
                f"pod home {home_dir} is not a plain directory; refusing to seed it: {exc}"
            ) from exc
        witness = pinned_fs.fd_real_path(home_fd)
        if not witness:
            # Fail CLOSED: with no witness there is nothing to validate the writes
            # against, which is the one thing that makes a by-name destination
            # acceptable here at all.
            raise PodError(
                f"could not read the real path of pod home {home_dir} from its own "
                "handle, so a seed written by name cannot be validated; refusing"
            )

        # ONE translation frame over BOTH paths, like the POSIX branch's single
        # ``try`` around its restart and fresh-seed blocks: an OSError from the
        # restart path (``iterdir``, ``_prepare_seeded_home_dir``) is a refusal in
        # exactly the same sense as one from the copy, and only ``PodError`` survives
        # this frame.
        try:
            if not fresh:
                # Same fresh / already-populated split the POSIX branch makes: a home
                # that already carries THIS scenario's completion marker is restarted
                # unchanged, and anything else is refused rather than overwritten.
                if not home_dir.is_dir():
                    raise PodError(
                        f"pod home {home_dir} is not a plain directory; refusing to seed it"
                    )
                if any(home_dir.iterdir()):
                    recorded = _seeded_scenario_in_dir(home_dir)
                    if recorded != scenario:
                        found = f"scenario {recorded!r}" if recorded else "no completion marker"
                        raise PodError(
                            f"pod home {home_dir} is populated but holds {found}, not "
                            f"requested scenario {scenario!r}; refusing to boot or overwrite "
                            f"it. Run `kirocrew pod down {name}` before retrying the seed."
                        )
                    _prepare_seeded_home_dir(home_dir)
                    if pinned_fs.fd_real_path(home_fd) != witness:
                        raise PodError(
                            f"pod home {home_dir} changed while it was being prepared for "
                            "restart; refusing to boot any path now present at that name"
                        )
                    return False

            seed_mod.copy_fixture_into_witnessed_dir(scenario, home_dir)
            _prepare_seeded_home_dir(home_dir)
            if pinned_fs.fd_real_path(home_fd) != witness:
                raise PodError(
                    f"pod home {home_dir} changed while it was being seeded; "
                    "refusing to boot any path now present at that name"
                )
            seed_mod.publish_fixture_manifest_into_witnessed_dir(scenario, home_dir)
        except (SeedError, OSError) as exc:
            # The frame now covers the RESTART path too, where the home is a complete
            # one this seed did not write, so "a partial home may remain" would be
            # false there -- and `pod down` DELETES the home, which is the wrong
            # advice for a transient failure over state worth keeping.
            raise PodError(
                f"seeding pod {name!r} from scenario {scenario!r} failed: {exc}. "
                f"If the home at {home_dir} is incomplete, reclaim it with "
                f"`kirocrew pod down {name}` before retrying -- a restart failure "
                "leaves the existing home intact, so retry that before nuking it."
            ) from exc
    finally:
        if home_fd >= 0:
            os.close(home_fd)
        pinned_fs.close_all(fds)
    return True


def _seeded_scenario_in_dir(home_dir: Path) -> str | None:
    """Return the completion marker from a caller-created pod home, by name.

    The win32 twin of :func:`_seeded_scenario_from_fd`. It reads a marker this
    process wrote under a directory it created, and a link at the marker's own name
    is refused rather than followed.
    """
    marker = home_dir / seed_mod.FIXTURE_MANIFEST
    if pinned_fs.is_reparse_point(marker):
        return None
    try:
        return _fixture_name_from_manifest_text(marker.read_text(errors="replace")[: 64 * 1024])
    except OSError:
        return None


def _prepare_seeded_home_dir(home_dir: Path) -> None:
    """Finish pod-owned setup on win32, under a home this process created.

    The win32 twin of :func:`_prepare_seeded_home_fd`. Every path it touches is a
    direct child of the caller-witnessed home, and each is screened for a reparse
    point before it is read or written, because a by-name read that follows a link
    would import config from outside the pod.
    """
    config = home_dir / "config.json"
    if pinned_fs.is_reparse_point(config):
        raise PodError(f"seeded config.json is a link; refusing to read it: {config}")
    data: dict = {}
    if config.is_file():
        try:
            text = config.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            raise PodError(f"could not read seeded config.json: {exc}") from exc
        if len(text) > 1024 * 1024:
            raise PodError("seeded config.json exceeds the 1 MiB pod setup limit")
        try:
            loaded = json.loads(text)
        except ValueError as exc:
            raise PodError(f"seeded config.json is not valid JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise PodError("seeded config.json must contain a JSON object")
        data = loaded

    runtime._apply_seed_config_floor(data)
    runtime.atomic_write(config, json.dumps(data, indent=2), fsync=True, mode=0o600)

    workspace = home_dir / "workspace"
    if pinned_fs.is_reparse_point(workspace):
        raise PodError(f"seeded workspace is a link; refusing to use it: {workspace}")
    workspace.mkdir(mode=0o700, exist_ok=True)


def seed_home_from_scenario(cfg: PodConfig, name: str, scenario: str) -> bool:
    """Populate pod *name* from *scenario* through a pinned home descriptor.

    The final home is created/opened relative to a pinned pod-root descriptor and
    every fixture entry is copied through that held directory. Nothing is
    published by renaming a deterministic staging name, so swapping a path entry
    cannot redirect writes into another pod. The fixture manifest is copied last
    and remains the completion marker: a failed partial copy is refused on the
    next ``up`` rather than treated as a completed seed.

    Windows reaches :func:`_seed_home_windows` instead, which states its own
    weaker-but-named guarantee: that platform has no ``dir_fd``, so a destination
    cannot be addressed relative to a held descriptor and the pinned walk this
    branch performs does not exist there. Every OTHER host without a pinned tree
    walk keeps the refusal below.
    """
    home_dir = cfg.home_dir(name)
    resolve_seed_scenario(scenario)

    if not pinned_fs.supports_pinned_tree_walk():
        if runtime.IS_WINDOWS:
            return _seed_home_windows(cfg, name, scenario, home_dir)
        raise PodError(
            "this host cannot pin a fixture copy to directory descriptors; "
            "refusing an unpinned pod seed"
        )

    try:
        root_fd = pinned_fs.create_and_open_dir_pinned(
            home_dir.parent,
            what="pod root",
            refusal=PodError,
        )
    except (OSError, PodError) as exc:
        if isinstance(exc, PodError):
            raise
        raise PodError(f"could not inspect or prepare pod home {home_dir}: {exc}") from exc

    home_fd = -1
    try:
        try:
            os.mkdir(home_dir.name, 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        try:
            home_fd = os.open(home_dir.name, pinned_fs.dir_flags(), dir_fd=root_fd)
        except OSError as exc:
            raise PodError(
                f"pod home {home_dir} is not a plain directory; refusing to seed it: {exc}"
            ) from exc
        try:
            entries = os.listdir(home_fd)
            if entries:
                recorded = _seeded_scenario_from_fd(home_fd)
                if recorded != scenario:
                    found = f"scenario {recorded!r}" if recorded else "no completion marker"
                    raise PodError(
                        f"pod home {home_dir} is populated but holds {found}, not "
                        f"requested scenario {scenario!r}; refusing to boot or "
                        "overwrite it. Run `kirocrew pod down "
                        f"{name}` before retrying the seed."
                    )
                _prepare_seeded_home_fd(home_fd)
                return False
            seed_mod.copy_fixture_into_dir_fd(scenario, home_fd)
            _prepare_seeded_home_fd(home_fd)
            seed_mod.publish_fixture_manifest(scenario, home_fd)
            held = os.fstat(home_fd)
            named = os.stat(home_dir.name, dir_fd=root_fd, follow_symlinks=False)
            if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
                raise PodError(
                    f"pod home {home_dir} changed while it was being seeded; "
                    "refusing to boot any path now present at that name"
                )
        except (SeedError, OSError) as exc:
            # Same wording as the win32 twin, and for the same reason: this frame
            # also covers the RESTART path above, where the home is a complete one
            # this seed did not write, so "a partial home may remain" would be false
            # -- and `pod down` DELETES the home, which is the wrong advice for a
            # transient failure over state worth keeping.
            raise PodError(
                f"seeding pod {name!r} from scenario {scenario!r} failed: {exc}. "
                f"If the home at {home_dir} is incomplete, reclaim it with "
                f"`kirocrew pod down {name}` before retrying -- a restart failure "
                "leaves the existing home intact, so retry that before nuking it."
            ) from exc
    finally:
        if home_fd >= 0:
            os.close(home_fd)
        os.close(root_fd)
    return True


def seeded_scenario_in_home(cfg: PodConfig, name: str) -> str | None:
    """Return the fixture name recorded in a seeded pod home, if present.

    On win32 the home is held through :func:`platform_compat.pin_directory` for
    the read (no rename or delete of it or its ancestors while the handle lives,
    and a reparse point at its name is refused by the open itself) and the marker
    is read by name under it through :func:`_seeded_scenario_in_dir`, the same
    pair the win32 seed branch writes with. Every other platform reads the marker
    through the pinned home descriptor.
    """
    home = cfg.home_dir(name)
    if runtime.IS_WINDOWS:
        try:
            home_fd = pin_directory(home)
        except OSError:
            return None
        try:
            return _seeded_scenario_in_dir(home)
        finally:
            os.close(home_fd)
    try:
        home_fd = pinned_fs.open_dir_pinned(
            home,
            what="seeded pod home",
            refusal=PodError,
        )
    except (OSError, PodError):
        return None
    try:
        return _seeded_scenario_from_fd(home_fd)
    finally:
        os.close(home_fd)


def write_pod_config(home_dir: Path, seed: str) -> None:
    """Ensure the pod HOME exists (owner-only) with a tunnel-disabled config.json.

    Every pod — blank or seeded — gets a config with ``tunnel.enabled=False`` so
    "never grabs the live Slack identity" is guaranteed by config, not merely by
    the absence of ``SLACK_*`` in the inherited env. The HOME dir is ``0o700`` and
    ``config.json`` is ``0o600`` — the seeded config can carry provider tokens /
    API keys, which must not be world-readable on a shared host.

    The seeded ``tunnel.enabled=False`` is NOT by itself what keeps a pod from
    publishing. This function is create-only (it returns early when ``config.json``
    already exists), and the value stays rewritable afterwards by anything that
    composes config — a provider, a migration, a hand edit. The enforcement is
    ``--no-tunnel`` on the boot argv, re-asserted at every exec. A target checkout
    whose gateway cannot parse that flag does not get the guarantee: it keeps this
    seeded value and behaves exactly as it did before the flag existed. See ``boot``.
    """
    runtime._ensure_pod_dir(home_dir, what="pod home")
    # The pod's own workspace root (see build_pod_env's KIROCREW_WORKSPACE).
    # Created here so the gateway never falls back to the live workspace.
    runtime._ensure_pod_dir(home_dir / "workspace", what="pod workspace")
    dst_cfg = home_dir / "config.json"
    # The create-only guard is an LSTAT, not ``exists()``. ``exists()`` follows a
    # link, so a link planted at ``config.json`` pointing at any existing host file
    # read as "already configured" and the pod booted on the attacker's file. A link
    # here is refused outright rather than treated as either absent or present.
    # Junction-aware: a Windows junction cannot alias a FILE, but a live one at
    # this name answered True to ``exists()`` and False to ``is_symlink()``, so
    # the guard read it as "already configured" and the pod booted with a
    # directory where its config should be; a dangling one answered False to
    # both and the create-only write then landed on the surviving entry.
    if is_link_or_junction(dst_cfg):
        raise PodError(f"refusing to seed pod config: {dst_cfg} is a symbolic link or junction")
    if dst_cfg.exists():
        return
    sanitized = runtime.sanitized_seed_config(Path(seed)) if seed else None
    cfg_data = sanitized if sanitized is not None else {"tunnel": {"enabled": False}}
    # Create-only (the guard above): lock the temp down before any token-bearing
    # payload reaches the published name. write_text then chmod left the file at its
    # inherited DACL until the chmod returned, and the chmod itself was a no-op on
    # Windows -- which is why this stays ``atomic_write(restrict_to_owner=True)``
    # rather than moving to the pinned publisher: that helper applies a POSIX mode,
    # and this file carries provider tokens on every platform. The link the pinned
    # path would have refused is refused by the lstat above instead.
    runtime.atomic_write(
        dst_cfg,
        json.dumps(cfg_data, indent=2),
        restrict_to_owner=True,
    )


# Suffixes of the two-file MCP OAuth grant PAIRS under ``.aws/sso/cache``, which
# :mod:`kiro_crew.mcp_grant` owns (``<sha256>.token.json`` /
# ``<sha256>.registration.json``). These are the ONLY names the seeding below
# refuses. They are per-server CREDENTIALS a Connect click mints: copying one
# forward would let a pod boot already "Connected" to a provider nobody consented
# to from inside it, and copying one back at teardown would leave a real grant on
# the host after the pod that minted it is gone. Restated here rather than
# imported at module scope because this runs on the gateway boot path; the values
# are asserted against ``mcp_grant``'s own constants by test.
#: The two-file MCP OAuth grant PAIRS (``<sha256>.token.json`` /
#: ``<sha256>.registration.json``) that :mod:`kiro_crew.mcp_grant` owns are the
#: ONLY thing a pod's ``.aws/sso/cache`` ever holds: the pod's own kiro-cli mints
#: them there and ``pod down`` reclaims them. Nothing is copied in from the host,
#: so no host bearer token exists in that tree for an agent shell to read.


def _runtime_auth_store_mappings() -> tuple[StoreMapping, ...]:
    """Source->staged mappings for the agent runtime's own identity stores.

    ``test_the_agent_runtime_auth_stores_stay_visible`` pins these OUT of every
    masking tier for a stated reason: "the agent runtime is itself spawned inside
    this sandbox and resolves its own access token from that store, so masking it
    would break the agent's model auth". Not masking it is only half the
    requirement -- under the pod's remapped ``HOME`` the store has to EXIST there
    too, which is what this staging supplies. Without it the child reaches
    kiro-cli's own login gate no matter what else the pod home contains.

    DERIVED from ``identity_stores.store_mappings`` rather than a hardcoded list,
    the same authoritative-table discipline ``acp.client``'s env scrub uses. An
    earlier revision hardcoded the two POSIX ``.local/share`` paths, so a macOS
    host (``~/Library/Application Support/...``) or a host with a redirected
    ``XDG_DATA_HOME`` staged NOTHING -- and the viability probe then ACCEPTED the
    resulting signed-out pod, because signed-out is a legitimate boot state. The
    table follows the env override on the SOURCE side and keeps the fixed default
    layout on the staged side, which is exactly what a pod needs: read from
    wherever the operator's store really is, write where the child will look.
    """
    return store_mappings(sys.platform, Path.home(), os.environ)


#: Runaway guard on ONE store's staging. The runtime's own identity store is small
#: by construction, so a tree that exceeds this is a sign the layout changed rather
#: than a case to serve; staging stops and the pod boots signed-out (loudly, via the
#: viability probe) instead of copying an unbounded tree on every boot.
_RUNTIME_AUTH_STORE_FILE_CAP = 512


#: Filename suffixes that make a staged file a SQLite DATABASE rather than bytes to
#: copy. Snapshotted through the backup API (see :func:`_snapshot_sqlite_pinned`), so
#: a live writer cannot hand the pod a torn generation.
_SQLITE_SUFFIXES: tuple[str, ...] = (".sqlite3", ".sqlite")


#: Sidecars a SQLite database keeps beside itself. NEVER staged: a backup already
#: contains every committed transaction they hold, and copying them alongside a
#: separately-copied main file is precisely what produced a mismatched set.
_SQLITE_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")


def _is_sqlite_sidecar(name: str) -> bool:
    """Is *name* a sidecar of a database this staging snapshots instead of copying?"""
    for sidecar in _SQLITE_SIDECAR_SUFFIXES:
        if name.endswith(sidecar):
            stem = name[: -len(sidecar)]
            if any(stem.endswith(suffix) for suffix in _SQLITE_SUFFIXES):
                return True
    return False


def _snapshot_sqlite_pinned(
    *,
    src_dir_fd: int,
    src_name: str,
    dst_dir_fd: int,
    dst_name: str,
) -> bool:
    """Stage ONE SQLite database as a consistent snapshot. True when it landed.

    **Why not a byte copy.** The host store belongs to a LIVE kiro-cli, and a
    WAL-mode database is a SET of files whose contents only agree at an instant.
    Copying ``data.sqlite3`` and then its ``-wal`` with two separate reads takes
    those files at two different times, so a checkpoint landing in between yields a
    main file from after it and a WAL from before -- a torn snapshot whose identity
    rows are missing or malformed, staged into the pod as if it were sign-in state.
    Nothing in a per-file copy loop can close that window, because the window is
    between the copies. SQLite's backup API reads the database through the engine
    under a read transaction, so what it writes is one generation by construction,
    and the sidecars need not be staged at all -- the result already contains every
    committed transaction they held.

    **How the pinned discipline survives an API that takes a path.** ``sqlite3``
    opens by NAME, which is the one thing this module refuses to do on a tree it
    does not own. The bridge is :func:`pinned_fs.fd_real_path`: both ends are opened
    as descriptors FIRST (source ``O_NOFOLLOW`` relative to the already-validated
    ``src_dir_fd``; destination ``O_CREAT | O_EXCL`` relative to ``dst_dir_fd``, so
    anything sitting at that name is a plant and creation refuses it rather than
    following it), and the path handed to ``sqlite3`` is then the KERNEL's own name
    for the inode already held open. That name has no symlink component left to
    swap, which is the property the descriptor discipline exists to get. Both opens
    fail closed: no descriptor, or no readable real path, and the store is refused.

    The mode is set with ``fchmod`` on the created descriptor -- before any bytes
    are written and on the fd rather than the name -- so the file is never briefly
    group-readable and the mode cannot land on some other inode.

    **Temp-then-rename, both inside the pinned destination directory.** A backup is
    not atomic, so an interrupted one leaves a short database that looks staged. The
    snapshot is built at a temp name and ``os.rename``d onto the real one with BOTH
    ``src_dir_fd`` and ``dst_dir_fd`` pinned to the same validated directory, so the
    name a reader can see either does not exist or is a complete snapshot, and the
    rename cannot be redirected out of the directory it was checked in. A leftover
    temp from a killed boot is cleaned up on the next attempt: it is created
    ``O_EXCL``, so a stale one is unlinked through the pinned fd first.

    Read-only on the source (``mode=ro``), so staging a pod can never write to the
    operator's live store.
    """
    import sqlite3

    tmp_name = f".{dst_name}.staging"
    src_fd: int | None = None
    dst_fd: int | None = None
    try:
        try:
            src_fd = os.open(
                src_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=src_dir_fd,
            )
        except OSError:
            return False
        if not stat.S_ISREG(os.fstat(src_fd).st_mode):
            return False
        src_real = pinned_fs.fd_real_path(src_fd)
        if not src_real:
            return False
        # A stale temp from an interrupted boot would defeat O_EXCL below. Removed
        # through the pinned fd, so the unlink cannot escape this directory.
        with contextlib.suppress(OSError):
            os.unlink(tmp_name, dir_fd=dst_dir_fd)
        try:
            dst_fd = os.open(
                tmp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dst_dir_fd,
            )
        except OSError:
            return False
        os.fchmod(dst_fd, 0o600)
        dst_real = pinned_fs.fd_real_path(dst_fd)
        if not dst_real:
            return False
        src_uri = f"file:{urllib.parse.quote(src_real)}?mode=ro"
        with contextlib.closing(sqlite3.connect(src_uri, uri=True)) as source:
            with contextlib.closing(sqlite3.connect(dst_real)) as target:
                source.backup(target)
        os.rename(tmp_name, dst_name, src_dir_fd=dst_dir_fd, dst_dir_fd=dst_dir_fd)
        return True
    except (OSError, sqlite3.Error):
        with contextlib.suppress(OSError):
            os.unlink(tmp_name, dir_fd=dst_dir_fd)
        return False
    finally:
        for fd in (src_fd, dst_fd):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)


def _stage_runtime_auth_store(os_home: Path, mapping: StoreMapping) -> int:
    """Mirror one HOME-relative runtime auth store into *os_home*. Best-effort.

    Same discipline as the SSO-cache staging above: every directory is created
    through a PINNED no-follow descriptor so a link planted at any component cannot
    redirect the copy, every level is forced to ``0o700`` and every file to
    ``0o600``, and copies are create-only so a pod that already refreshed its own
    credential is not clobbered.

    A SQLite database is SNAPSHOTTED rather than copied, and its ``-wal`` / ``-shm``
    / ``-journal`` sidecars are skipped entirely -- see
    :func:`_snapshot_sqlite_pinned` for why a per-file copy of a live database's
    file set cannot be consistent. Databases in a directory are handled BEFORE its
    plain files, and a database that cannot be snapshotted REFUSES THE WHOLE STORE
    (returns 0): the token is what the store is for, so a tree staged without it
    would present a pod as provisioned while every agent turn failed to sign in.
    Because ``os.walk`` is top-down and the database sits at the store root, that
    refusal lands before anything has been staged rather than half-way through.

    Returns the number of files staged. Never raises: a missing or unreadable host
    store just means the pod boots signed-out, which the boot-time viability probe
    reports. The whole tree is mirrored rather than a chosen filename, because this
    store's internal layout is the runtime's own contract (kiro-cli resolves its
    token from it through the ``dirs`` crate) and guessing a name is what made the
    SSO-cache staging a silent no-op in an earlier revision.
    """
    source_root = mapping.source
    parts = mapping.staged_relative.parts
    staged = 0
    if not pinned_fs.supports_pinned_walk():
        # No ``O_DIRECTORY``/``O_NOFOLLOW``/``dir_fd`` on this platform (Windows).
        # Every write below goes through a pinned no-follow descriptor precisely
        # because it moves sign-in material, so there is no by-name fallback to
        # degrade to -- see ``pinned_fs.supports_pinned_walk``. Callers inside
        # ``_seed_pod_os_home`` are already past that platform refusal; this guard
        # is what makes the helper safe to call (and to unit-test) directly.
        return 0
    fds: list[int] = []
    try:
        try:
            source_root_fd = pinned_fs.open_dir_pinned(
                source_root, what=f"host {parts[-1]} identity store", refusal=PodError
            )
        except (OSError, PodError):
            return 0  # no such store on this host; nothing to mirror
        fds.append(source_root_fd)
        for dirpath, dirnames, filenames in os.walk(source_root):
            rel = Path(dirpath).relative_to(source_root)
            # Recreate this level under the pod home, pinned at every component.
            target = os_home
            try:
                for label in (*parts, *rel.parts):
                    target = target / label
                    level_fd = pinned_fs.create_and_open_dir_pinned(
                        target, what=f"pod auth store {label}", refusal=PodError
                    )
                    fds.append(level_fd)
                    os.fchmod(level_fd, stat.S_IRWXU)
            except (OSError, PodError):
                dirnames[:] = []  # this subtree is unsafe or unwritable; skip it
                continue
            dst_dir_fd = fds[-1]
            try:
                src_dir_fd = pinned_fs.open_dir_pinned(
                    Path(dirpath), what=f"host {parts[-1]} identity store", refusal=PodError
                )
            except (OSError, PodError):
                dirnames[:] = []
                continue
            fds.append(src_dir_fd)
            # Databases first, so a refusal happens before anything is staged.
            names = sorted(filenames)
            databases = [n for n in names if n.endswith(_SQLITE_SUFFIXES)]
            for name in databases:
                if pinned_fs.stat_at(dst_dir_fd, name) is not None:
                    continue  # create-only, exactly like the copy path
                if not _snapshot_sqlite_pinned(
                    src_dir_fd=src_dir_fd,
                    src_name=name,
                    dst_dir_fd=dst_dir_fd,
                    dst_name=name,
                ):
                    print(
                        f"kirocrew-pod: refusing auth store {'/'.join(parts)}: "
                        f"{name} could not be snapshotted consistently"
                    )
                    return 0
                staged += 1
            for name in names:
                if name in databases or _is_sqlite_sidecar(name):
                    continue
                if staged >= _RUNTIME_AUTH_STORE_FILE_CAP:
                    print(
                        f"kirocrew-pod: auth store {'/'.join(parts)} exceeded "
                        f"{_RUNTIME_AUTH_STORE_FILE_CAP} files; staging stopped"
                    )
                    return staged
                try:
                    pinned_fs.copy_file_pinned(
                        str(Path(dirpath) / name),
                        dir_fd=src_dir_fd,
                        name=name,
                        dst_dir_fd=dst_dir_fd,
                        dst_name=name,
                        skip_existing=True,
                        force_mode=0o600,
                    )
                except OSError:
                    continue
                staged += 1
    finally:
        pinned_fs.close_all(fds)
    return staged


def _pin_outermost_existing_windows(target: Path, *, what: str) -> tuple[int, Path]:
    """Pin the deepest ANCESTOR of *target* that already exists, before creating.

    The win32 stand-in for the anchor a ``dir_fd`` walk gets for free, and the one
    level :func:`_pin_created_dir_windows` cannot supply: it demands an
    already-pinned parent, so something has to pin the first one.

    ``platform_compat.pin_directory`` opens without ``FILE_SHARE_DELETE``, and
    that freezes the directory it opens AND every directory above it for the
    handle's lifetime — so pinning the deepest existing ancestor is what makes the
    whole chain above unrenamable while the levels below are created. Doing it in
    the other order is the hole this function closes: a by-name
    ``mkdir(parents=True)`` walks and creates through ancestors nothing is holding,
    so a same-UID process that swaps one between the screen and the first pin has
    the rest of the build land in its own tree.

    The open refuses to follow a reparse point, so a junction planted at the
    anchor's name fails the open rather than being pinned in its target's place.

    Returns the pin and the anchor it names. The caller creates every level from
    there down through :func:`_pin_created_dir_windows`, so no level is ever
    created under an unpinned parent.

    RESIDUAL: the anchor is still located BY NAME, and no witness can close that —
    a witness taken before the pin can only be compared against itself. What the
    pin rules out is a reparse point at the anchor and any rename of it or of
    anything above it from that moment on; what remains is a same-UID rename-swap
    landing in the instant before the pin, which is the operational-isolation
    boundary the pod threat model already records rather than a new one.
    """
    anchor = target.parent
    while not anchor.is_dir() and anchor != anchor.parent:
        anchor = anchor.parent
    if pinned_fs.is_reparse_point(anchor):
        raise PodError(
            f"refusing to build {what} under {anchor}: it is a symbolic link or a "
            "junction, so the path does not name the directory it appears to"
        )
    return pin_directory(anchor), anchor


def _pin_created_dir_windows(parent_fd: int, target: Path, *, what: str) -> int:
    """Create *target* under an already-pinned parent and return its own pin.

    The win32 stand-in for ``pinned_fs.create_and_open_dir_pinned``, which needs
    ``dir_fd`` and so cannot run here. Three steps, in this order, and the order
    is the guarantee: the parent is ALREADY pinned by the caller (so it can be
    neither renamed nor deleted, and neither can anything above it, for as long
    as that handle lives), the child name is screened with
    ``pinned_fs.is_reparse_point`` before it is created, and
    ``platform_compat.pin_directory`` then opens the child WITHOUT following a
    reparse point, so a junction planted at the name between the screen and the
    open fails the open instead of being pinned in its target's place.

    ``parent_fd`` is taken rather than read, so a caller that has not pinned the
    parent cannot reach this. A mode argument is deliberately absent: NTFS
    carries no POSIX mode, ``os.fchmod`` does not exist on this platform, and the
    ancestor pin plus the plane root's own ACL is what bounds who can reach the
    tree -- see the caller for what that costs.
    """
    if parent_fd < 0:  # pragma: no cover - caller bug, never a runtime state
        raise PodError(f"refusing to create {what} without a pinned parent")
    if pinned_fs.is_reparse_point(target):
        raise PodError(
            f"refusing to build {what} at {target}: it is a symbolic link or a junction, "
            "so the path does not name the directory it appears to"
        )
    target.mkdir(exist_ok=True)
    return pin_directory(target)


def _seed_pod_os_home_windows(os_home: Path) -> None:
    """Build the pod OS home on win32, stating what it proves and what it does not.

    **The guarantee, and the ORDER is the guarantee.** The deepest ancestor of
    *os_home* that already exists is screened for a reparse point and PINNED
    first, which freezes it and every directory above it for as long as the handle
    lives. Only then is each remaining level — down through *os_home* and on
    through ``.aws/sso/cache`` — created under the pin of the level above it and
    immediately pinned itself with ``platform_compat.pin_directory``, which opens
    without ``FILE_SHARE_DELETE`` and refuses to follow a reparse point, so a
    junction planted at a name fails the open rather than being pinned in its
    target's place. No level is created under a parent nothing is holding. Every
    handle is held until the whole tree exists and the staging is finished, and
    they are closed in one place.

    The earlier order screened the ancestors by name and then walked them with
    ``mkdir(parents=True)``, taking its first pin at *os_home* itself: every level
    below obeyed "create under a pinned parent" and the outermost level, the only
    one with no parent pin to inherit, did not. A same-UID process that swapped an
    ancestor in that window had this function build the pod's whole OAuth grant
    corridor inside its directory and return normally — with no witness able to
    notice, since the witness compares a value only against itself.

    **The mode tightening is a no-op here, and that is stated rather than
    emulated.** The POSIX branch forces every level to ``0o700`` through
    ``os.fchmod``, which this platform does not implement, and NTFS carries no
    POSIX mode for it to set. What bounds who can reach this tree is the pod plane
    root's own ACL plus the ancestor pins above, not a mode bit. Do not "fix" this
    by calling ``os.chmod``: on Windows that touches only the read-only attribute,
    so it would read as a permission tightening while granting nothing.

    **The residual, named rather than implied.** Two things the pins do not do.
    They do not stop a same-UID process from writing INTO these directories, and
    they do not extend past this function: the child receives ``os_home`` as a
    path and re-resolves it at its own open. And the anchor is still LOCATED by
    name, so a same-UID rename-swap landing in the instant before the first pin is
    not excluded — no witness closes that, because a witness taken before the pin
    can only be compared against itself. What the pin does exclude, from that
    moment on, is a reparse point at any level and any rename or delete of these
    directories or of anything above them. A pod home lives under a plane root
    only this user can write, so the residual is the same trust domain the OS
    already grants that user, and the same operational isolation the pod threat
    model records -- a pod is not protection from arbitrary same-UID processes.
    Closing it would need the child to accept a descriptor instead of a path,
    which the kiro-cli interface does not offer.

    Raises :class:`PodError` when the tree cannot be built, and that refusal
    aborts the boot: this directory becomes the pod child's ``HOME``, so an
    unverified one is the machine-level grant writer the whole mechanism exists to
    prevent. Staging the sign-in material stays best-effort, so a signed-out host
    boots a pod that can prompt for sign-in inside it.
    """
    _refuse_reparse_chain(os_home)
    fds: list[int] = []
    try:
        try:
            # Pin BEFORE creating. The old order screened the ancestors by name
            # and then walked them with mkdir(parents=True), taking its first pin
            # only at ``os_home`` itself — so every level below obeyed "create
            # under a pinned parent" and the outermost one, the only level that
            # had no parent pin to inherit, did not. A same-UID process that
            # swapped an ancestor in that window had this function build the pod's
            # whole OAuth grant corridor inside its directory and return normally.
            anchor_fd, anchor = _pin_outermost_existing_windows(os_home, what="the pod OS home")
            fds.append(anchor_fd)
            # Create every remaining level from the anchor DOWN, each under the
            # pin of the level above it, so no directory is ever created under a
            # parent nothing is holding.
            target = anchor
            for part in os_home.relative_to(anchor).parts:
                target = target / part
                fds.append(_pin_created_dir_windows(fds[-1], target, what=f"pod OS home {part}"))
            for label in (".aws", "sso", "cache"):
                target = target / label
                fds.append(_pin_created_dir_windows(fds[-1], target, what=f"pod OS home {label}"))
        except OSError as exc:
            # The directory the child would receive as HOME does not exist or is
            # not ours, so this is the same fail-closed case as a refused
            # component rather than a skippable seed.
            raise PodError(f"could not build the pod OS home under {os_home}: {exc}") from exc
        # ---- BEST-EFFORT from here: the tree is sound, only the staging can fail.
        for mapping in _runtime_auth_store_mappings():
            staged = _stage_runtime_auth_store_windows(os_home, mapping)
            if staged:
                print(
                    f"kirocrew-pod: staged {staged} file(s) from "
                    f"{mapping.staged_relative.as_posix()}"
                )
        # Nothing host-derived is staged into ``<os-home>/.aws/sso/cache`` on any
        # platform: it is created so the pod's own kiro-cli writes its MCP OAuth
        # grants there, and that is all it ever holds.
    finally:
        pinned_fs.close_all(fds)


def _stage_runtime_auth_store_windows(os_home: Path, mapping: StoreMapping) -> int:
    """Mirror one runtime auth store into *os_home* on win32. Best-effort.

    The win32 twin of :func:`_stage_runtime_auth_store`, and it keeps the two
    properties that matter while dropping the one this platform cannot express:

    * The SOURCE stays pinned. Each file is opened once and handed to
      ``pinned_fs.copy_file_pinned`` as ``src_fd``, documented as the only pinned
      source form here, so the bytes copied are the bytes of the inode that open
      reached rather than of whatever the name means afterwards.
    * Every DESTINATION is an ``O_CREAT | O_EXCL`` create under a directory this
      function pinned, so it can only add files it created. That is also what
      keeps the copy create-only: an occupied name is skipped, so a pod that has
      already refreshed its own credential is never clobbered.
    * What it cannot keep is destination ancestor pinning by ``dir_fd``. Each
      level is created under its pinned parent and pinned itself, which blocks a
      rename or a delete of it, and the final open is still by name.

    A SQLite database is skipped along with its sidecars rather than snapshotted:
    ``_snapshot_sqlite_pinned`` copies through ``dir_fd`` descriptors, and a
    per-file copy of a live database's file set cannot be consistent. A store
    whose token lives in a database therefore stages nothing and the pod boots
    signed-out, which the boot-time viability probe reports.

    Returns the number of files staged, and never raises: an unreadable host
    store just means the pod prompts for sign-in inside itself.
    """
    parts = mapping.staged_relative.parts
    staged = 0
    fds: list[int] = []
    try:
        source_root = mapping.source
        if not source_root.is_dir() or pinned_fs.is_reparse_point(source_root):
            return 0
        for dirpath, dirnames, filenames in os.walk(source_root):
            # ``os.walk`` DESCENDS into a junction on this platform: a directory
            # reparse point is just a directory to it. The file loop below screens
            # each leaf, but a junction planted anywhere under the host's auth store
            # would be stepped into before any leaf is reached, and its target's
            # contents staged into the pod as though they were sign-in material.
            # Prune here, at the entry to the body, because by the time a leaf is
            # screened the traversal itself has already happened.
            dirnames[:] = [d for d in dirnames if not pinned_fs.is_reparse_point(Path(dirpath) / d)]
            rel = Path(dirpath).relative_to(source_root)
            level_fds: list[int] = []
            target = os_home
            try:
                parent_fd = pin_directory(os_home)
            except OSError:
                return staged
            fds.append(parent_fd)
            try:
                for label in (*parts, *rel.parts):
                    target = target / label
                    parent_fd = _pin_created_dir_windows(
                        parent_fd, target, what=f"pod auth store {label}"
                    )
                    level_fds.append(parent_fd)
                    fds.append(parent_fd)
            except (OSError, PodError):
                dirnames[:] = []  # this subtree is unsafe or unwritable; skip it
                continue
            if not level_fds:  # pragma: no cover - parts is never empty
                continue
            for name in sorted(filenames):
                if name.endswith(_SQLITE_SUFFIXES) or _is_sqlite_sidecar(name):
                    # A live database cannot be copied file by file consistently,
                    # and the snapshot helper needs dir_fd descriptors.
                    continue
                if staged >= _RUNTIME_AUTH_STORE_FILE_CAP:
                    print(
                        f"kirocrew-pod: auth store {'/'.join(parts)} exceeded "
                        f"{_RUNTIME_AUTH_STORE_FILE_CAP} files; staging stopped"
                    )
                    return staged
                src = Path(dirpath) / name
                try:
                    # NOT ``os.open(..., getattr(os, "O_NOFOLLOW", 0))``: that flag
                    # does not exist here, so the fallback is 0 and the open FOLLOWS
                    # a reparse point at the leaf — a control that reads as portable
                    # and enforces nothing. Screening first and opening after is no
                    # better: an adversary that can plant the link chooses when.
                    # ``open_file_no_reparse`` refuses it in the SAME operation.
                    src_fd = open_file_no_reparse(src)
                except OSError:
                    continue
                try:
                    copied = pinned_fs.copy_file_pinned(
                        str(src),
                        str(target / name),
                        src_fd=src_fd,
                        skip_existing=True,
                        force_mode=0o600,
                    )
                except (OSError, ValueError):
                    continue
                if copied:
                    staged += 1
    finally:
        pinned_fs.close_all(fds)
    return staged


def _seed_pod_os_home(os_home: Path) -> None:
    """Create-only: stage the agent runtime's identity store into *os_home*.

    This is what lets a sign-in performed ONCE on the operator's real machine be
    reused by every pod, while a pod's own MCP OAuth grants stay confined to
    ``os_home`` -- see ``build_pod_env``'s
    ``KIROCREW_OS_HOME`` docstring for the split this closes. The sign-in material
    staged is the AGENT RUNTIME's own identity store
    (``_runtime_auth_store_mappings``, derived from ``identity_stores``);
    the ``.aws/sso/cache`` tree is CREATED empty and never populated from the host,
    so the pod's grant corridor holds only what the pod itself mints.

    **Every component is created and opened through a PINNED no-follow
    descriptor, never by name.** This function copies a HOST credential into a
    tree under the pod root, and it runs again on every boot -- so a name-based
    ``mkdir(parents=True)`` plus a by-name write would follow a symlink planted
    at ``os-home`` (or at any component beneath it) and deposit the operator's
    SSO token wherever that link pointed, including an agent-readable workspace.
    ``pinned_fs.create_and_open_dir_pinned`` refuses a link at the component it
    creates and pins the parent chain first, and ``pinned_fs.copy_file_pinned``
    validates the descriptor it copies rather than the name, so the inode
    written is the inode checked. This is the same discipline
    ``seed_home_from_scenario`` in this module already applies to a seeded home.

    Create-only and per-file: ``skip_existing`` leaves an existing destination
    untouched (a pod that already signed in, or already refreshed its own token,
    is not clobbered), and a missing or unreadable source token is skipped
    rather than aborting the whole pod boot -- a signed-out host still boots a
    pod that can prompt for sign-in inside it, which is strictly better than
    refusing to boot at all. The staged token is forced to ``0o600`` and every
    directory to ``0o700``, so a token never lands world-readable even if the
    source file's own mode is looser.

    **Raises PodError when the TREE ITSELF cannot be built through pinned
    no-follow descriptors, and that refusal must abort the boot** (``boot`` does
    exactly this). The two phases are deliberately NOT equally forgiving:

    * Building the tree is MANDATORY. This directory becomes the pod child's
      ``HOME`` (``build_pod_env`` exports it as ``KIROCREW_OS_HOME``,
      ``acp.client._apply_pod_home_remap`` assigns it), so if a component is a
      planted symlink the refusal here is the ONLY thing standing between the
      pod's kiro-cli and the real host tree the link points at. An earlier
      revision swallowed this refusal and booted anyway: nothing was written
      through the link by THIS function, but the child then received the refused
      path as its ``HOME`` and wrote its own MCP OAuth grants through the link --
      turning the pod back into the machine-level grant writer this whole
      mechanism exists to prevent. Skipping the seed is safe; booting on an
      unverified ``HOME`` is not, so the two outcomes must not share a branch.
    * Copying the tokens is BEST-EFFORT, unchanged. An unreadable host cache
      (signed out, permission error, stalled mount) and an individual token that
      cannot be copied both leave the pod booting signed-out.

    Every component is chmodded to ``0o700`` rather than only the leaf, so no
    level of the path this credential lands under is group- or world-writable --
    that is the narrowest replacement window the pinned primitives allow. The
    residual is a genuine TOCTOU: a same-UID process can still swap a component
    between this function returning and the child's ``exec``. Per the recorded
    threat model a pod is operational isolation, not protection from arbitrary
    same-UID processes, so that window is documented rather than claimed closed;
    closing it would require handing the child a descriptor instead of a path,
    which the kiro-cli interface does not accept.

    Windows reaches :func:`_seed_pod_os_home_windows` instead, which states its own
    narrower guarantee: that platform has no ``O_DIRECTORY``/``O_NOFOLLOW`` and no
    ``dir_fd``, so it pins each level with ``platform_compat.pin_directory`` (which
    blocks a rename or a delete of the directory and of everything above it, and
    refuses to follow a reparse point) and says plainly that the mode tightening
    has no equivalent there. Every OTHER host without a pinned walk keeps the
    refusal below.
    """
    if not pinned_fs.supports_pinned_walk():
        if runtime.IS_WINDOWS:
            _seed_pod_os_home_windows(os_home)
            return
        # No O_DIRECTORY/O_NOFOLLOW on this platform, so the tree cannot be built
        # through pinned no-follow descriptors. REFUSE rather than fall back to a
        # by-name copy: this moves a HOST credential, and an unpinned write is
        # exactly the symlink-redirect the pinning exists to prevent. Refusing to
        # SEED is not enough on its own, because the same unverified directory
        # would still become the child's HOME -- so this is raised, not returned,
        # and the boot stops.
        raise PodError(
            f"pod OS home {os_home} needs O_DIRECTORY/O_NOFOLLOW descriptors to be "
            "built safely and this platform provides none"
        )
    fds: list[int] = []
    try:
        # Each level is created through its PINNED parent, so a link planted at
        # any component is refused instead of followed. Passing the full path
        # per level is deliberate: create_and_open_dir_pinned pins the whole
        # ancestor chain itself and creates only the final component.
        try:
            target = os_home
            for label in ("os-home", ".aws", "sso", "cache"):
                if label != "os-home":
                    target = target / label
                fds.append(
                    pinned_fs.create_and_open_dir_pinned(
                        target, what=f"pod OS home {label}", refusal=PodError
                    )
                )
                # Tighten EVERY level, not just the leaf: a group-writable
                # ancestor is a replacement window for the credential below it.
                os.fchmod(fds[-1], stat.S_IRWXU)
        except OSError as exc:
            # ENOSPC/EACCES/EIO building the tree. The directory the child would
            # receive as HOME does not exist or is not ours, so this is the same
            # fail-closed case as a refused component, not a skippable seed.
            raise PodError(f"could not build the pod OS home under {os_home}: {exc}") from exc
        # ---- BEST-EFFORT from here: the tree is sound, only the staging can fail.
        # Every failure below leaves the pod booting signed-out, which is why none
        # of them may escape as the PodError that aborts the boot.
        #
        # The runtime's OWN identity store comes first because it is what decides
        # whether the child is signed in at all: kiro-cli resolves its access token
        # from its own identity store (see ``_runtime_auth_store_mappings``), not from
        # the SSO cache below. Staging only the cache is what left the child at
        # kiro-cli's login gate with a readable, correctly-unmasked corridor.
        for mapping in _runtime_auth_store_mappings():
            staged = _stage_runtime_auth_store(os_home, mapping)
            if staged:
                print(
                    f"kirocrew-pod: staged {staged} file(s) from "
                    f"{mapping.staged_relative.as_posix()}"
                )
        # NOTHING host-derived is staged into `<os-home>/.aws/sso/cache`. The
        # directory is created (above, pinned and 0o700) because the pod's own
        # kiro-cli writes its MCP OAuth grants there, and that is ALL it ever
        # holds. An earlier revision copied the host's SSO tokens in, which a
        # security review correctly flagged: the corridor that keeps the grant
        # store writable also made those copied HOST bearer tokens readable to any
        # agent shell in the pod. Live acceptance settled that the copy was never
        # load-bearing -- sign-in comes from the runtime's own data store staged
        # just above, not from this cache -- so the copy is deleted rather than
        # hidden. Deleting the material beats masking it from the process that has
        # to write beside it.
    finally:
        pinned_fs.close_all(fds)


def resolved_pod_home(cfg: PodConfig, name: str) -> Path:
    """Pod *name*'s HOME as :func:`cleanup_home` reports it.

    Teardown messages must agree on one spelling of the path. ``pod_home`` returns
    it unresolved, while ``cleanup_home`` resolves before deleting (its safety
    check needs the real parent), so quoting both in one failure read as two
    different directories wherever ``$HOME`` is a symlink — which is the default
    layout on a dev desktop.
    """
    try:
        return (cfg.pod_root / name).resolve()
    except OSError:
        return runtime.pod_home(cfg, name)


def orphan_homes(cfg: PodConfig) -> list[str]:
    """Pod HOMEs left on disk with no live pod and no installed definition.

    Reachable on BOTH platforms, because neither reclaims from a post-stop service
    hook any more (see :func:`stop_pod`): a pod that goes away without an explicit
    ``down`` — a crash, a raw ``systemctl --user stop`` / ``launchctl bootout``, a
    host reboot — leaves its isolated HOME behind. Reported rather than deleted so
    the operator decides, and so the delete still routes through
    :func:`cleanup_home`'s re-validation via ``kirocrew pod down <name>``.
    """
    try:
        # never follow a link: a link under pod_root can point at a LIVE
        # pod's HOME (or anywhere), and everything downstream of this
        # enumeration treats the NAME as the directory it will judge and
        # delete. A real pod HOME is always created as a plain directory.
        # Junction-aware, not ``is_symlink()``: on unelevated Windows the only
        # link a same-user writer CAN plant is a directory junction, which
        # answers True to ``is_dir()`` and False to ``is_symlink()`` -- so an
        # ``is_symlink()`` filter listed exactly the planted alias as an
        # orphan, and the operator's `pod down <alias>` then judged the live
        # sibling it points at. Link test FIRST: it is an lstat, so a link is
        # rejected before ``is_dir()`` would stat THROUGH it -- on Windows a
        # link whose target is a UNC share turns that stat into an outbound
        # SMB connection that authenticates as this process.
        entries = [p for p in cfg.pod_root.iterdir() if not is_link_or_junction(p) and p.is_dir()]
    except OSError:
        return []
    live = runtime.active_names(cfg)
    out = []
    for p in entries:
        if p.name.startswith("."):
            continue
        if p.name in live:
            continue
        # macOS writes a per-pod plist at `up` and drops it at `down`, so its
        # presence means the pod is installed rather than orphaned. Windows does
        # the same with its per-pod `.cmd` wrapper. systemd's template unit is
        # machine-wide, so liveness is the only signal there.
        if runtime.IS_MACOS and launchd.plist_path(cfg, p.name).exists():
            continue
        if runtime.IS_WINDOWS and win_backend.task_script_path(cfg, p.name).exists():
            continue
        out.append(p.name)
    return sorted(out)


#: How many times :func:`cleanup_home` re-attempts the removal, and how long it
#: pauses between attempts. Windows releases a dead process's handles
#: ASYNCHRONOUSLY, so the instant after a gateway exits its HOME is still
#: undeletable even though nothing is running: MEASURED on a native host, killing
#: a holder and deleting immediately leaves the tree in place, and a retry clears
#: it on the SECOND attempt (~100 ms later). A control with no holder deletes on
#: the first attempt, and a holder that is still ALIVE survives every attempt — so
#: this absorbs the OS's release latency without weakening the survivor check
#: below, which still reports a tree that genuinely cannot be reclaimed.
_HOME_RECLAIM_ATTEMPTS = 10


_HOME_RECLAIM_PAUSE_SECS = 0.1


def _rmtree_bounded(unresolved: Path) -> bool:
    """Remove *unresolved* by NAME, retrying while the OS releases handles.

    Returns True when the entry is gone. Always deletes by the unresolved name for
    the reason the caller documents (a swap to a symlink mid-delete must make
    ``rmtree`` refuse rather than follow), and re-checks with ``lexists`` so an
    entry swapped to a dangling link is not mistaken for a clean reclaim.
    """
    for attempt in range(_HOME_RECLAIM_ATTEMPTS):
        shutil.rmtree(unresolved, ignore_errors=True)
        if not os.path.lexists(unresolved):
            return True
        if is_link_or_junction(unresolved):
            # A swap happened; retrying cannot help and must not be attempted —
            # the caller reports the link itself as the residue. Junction-aware
            # for the same reason as the pre-check in cleanup_home: stdlib rmtree
            # refuses a junction root exactly as it refuses a symlink, so the
            # retry loop would otherwise spin its whole window on an entry that
            # can never be reclaimed.
            return False
        if attempt + 1 < _HOME_RECLAIM_ATTEMPTS:
            time.sleep(_HOME_RECLAIM_PAUSE_SECS)
    return not os.path.lexists(unresolved)


def cleanup_home(cfg: PodConfig, name: str) -> int:
    """Delete pod *name*'s isolated HOME and report whether it is really gone.

    Routed through Python (not a raw ``rm -rf {pod_root}/<name>``) because the rm
    safety must NOT rely on a service manager's instance-name semantics: a systemd
    ``%i`` cannot contain ``/`` but CAN be ``..``, and the template unit is a
    standalone artifact that bypasses the CLI's ``validate_name``. Re-validate the
    name and confirm the target is a direct child of pod_root before deleting, so
    teardown can never escape to ``$HOME`` or a parent.

    Returns 0 only when the directory is gone afterwards. ``rmtree`` runs with
    ``ignore_errors`` — it has to, since a partially-removed tree is still progress
    — so the removal itself is silent, and it is RETRIED within a bounded window
    because on Windows the handles of a process that has already exited are
    released asynchronously: the delete that runs immediately after a gateway goes
    away can fail on a tree nothing is using. A tree that survives the whole window
    (a live process holds it, or recreated it in append mode right behind the
    delete) returns 1 and names what is left. Without that check a caller cannot
    tell a reclaimed HOME from a swallowed failure; without the retry it cannot
    tell a locked HOME from a slow one.
    """
    try:
        runtime.validate_name(name)
    except PodError:
        print(f"refusing pod cleanup for invalid instance name {name!r}")
        return 2
    root = cfg.pod_root.resolve()
    unresolved = cfg.pod_root / name
    # Refuse to delete THROUGH a link: resolving first lets a link planted
    # under pod_root pass the containment check below while the tree it names
    # lives elsewhere — including another, live pod's HOME. A real pod HOME is
    # always created as a plain directory, so a link here is never ours to follow.
    # Junction-aware, because on unelevated Windows a junction is the only link a
    # same-user writer can plant, and ``is_symlink()`` answers False for it: the
    # alias then resolved to the live sibling, passed containment, and the
    # failure below was reported as "something is still writing there" instead
    # of as the planted link it was.
    if is_link_or_junction(unresolved):
        print(f"refusing pod cleanup: {unresolved} is a symlink or junction, not a pod HOME")
        return 2
    target = unresolved.resolve()
    if target == root or target.parent != root:
        print(f"refusing pod cleanup: {target} is not a pod dir under {root}")
        return 2
    # Delete by the UNRESOLVED name, never the resolved target: between the
    # symlink pre-check above and this call the entry can be swapped for a
    # symlink (check-to-use race), and rmtree on the RESOLVED path would then
    # delete the live sibling the link points at. rmtree itself refuses a
    # top-level symlink, so deleting by name makes the swap harmless — nothing
    # is removed and the survivor check below reports the failure.
    #
    # Bounded RETRY, because "still there" and "cannot be reclaimed" are not the
    # same state on Windows: handles of a process that has already exited are
    # released asynchronously, so the delete that runs immediately after a gateway
    # goes away fails on a tree nothing is using. See _rmtree_bounded for the
    # measurement. A tree a LIVE process holds survives every attempt and still
    # reports below, so no failure is hidden — only the OS's own latency is.
    if _rmtree_bounded(unresolved):
        return 0
    # Verify by the ENTRY itself, never the resolved target: an entry swapped
    # to a DANGLING symlink during the delete makes rmtree refuse silently
    # (suppressed by ignore_errors), and the resolved target of a dangling
    # link does not exist — so a target-existence check would report a clean
    # reclaim while the link remains as residue that orphan_homes (which
    # skips symlinks) can never surface again.
    if is_link_or_junction(unresolved):
        # Swapped to a link mid-delete: rmtree refused it (correctly), and
        # the link itself is the residue — name it rather than the target.
        print(
            f"pod cleanup did not remove {unresolved}: the entry is now a "
            "symlink or junction, which teardown refuses to follow — remove it by hand"
        )
        return 1
    survivors = _surviving_entries(target)
    print(
        f"pod cleanup did not fully remove {target}: still present "
        f"({', '.join(survivors)}) — either something is still writing there or "
        "the tree cannot be unlinked (permissions)"
    )
    return 1


def _surviving_entries(target: Path, limit: int = 5) -> list[str]:
    """Names of the first few entries left under a HOME that survived teardown.

    Diagnostics only: the point is to name a culprit ("security_events.jsonl")
    rather than report a bare failure, so an unlistable directory degrades to the
    directory itself instead of raising inside teardown.
    """
    try:
        names = sorted(p.name for p in target.iterdir())
    except OSError:
        return [target.name]
    if not names:
        return [f"{target.name} (empty)"]
    if len(names) > limit:
        return [*names[:limit], f"… +{len(names) - limit} more"]
    return names
