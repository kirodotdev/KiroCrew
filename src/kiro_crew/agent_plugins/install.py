"""Install and uninstall Agent Plugins in the shared powers home.

An install copies a plugin into ``<powers home>/installed/<name>/`` and lists it
in ``installed.json``, the way Kiro IDE and kiro-cli do, so the plugin also
appears in Kiro. A folder already in the standard layout is copied; any other is
converted with :mod:`kiro_crew.agent_plugins.convert`. Either way the result is
validated with the loader before anything under ``installed/`` changes, and a
new plugin lands ``off``: installing never turns a plugin on, and nothing here
runs code from the package.

Every write is atomic. The plugin folder is built in a hidden staging folder
inside ``installed/`` and moved into place with ``os.replace``; ``installed.json``
is rewritten with :func:`kiro_crew.atomic_write.atomic_write` under a lock, keeping
every other entry and every field this module does not know. ``data/<name>/`` is
never touched. Spec: ``docs/system-specs/modules/agent-plugins.md`` §5.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterator
from urllib.parse import urlsplit

from kiro_crew.agent_plugins.convert import (
    ConversionError,
    ConversionReport,
    _copy_package,
    convert_plugin,
    find_client_manifest,
)
from kiro_crew.agent_plugins.discovery import LEGACY_POWER_FILE, MAX_INDEX_BYTES, inspect_plugin
from kiro_crew.agent_plugins.manifest import MANIFEST_FILE, ManifestError, load_manifest
from kiro_crew.agent_plugins.model import PluginRecord
from kiro_crew.agent_plugins.paths import (
    INSTALLED_DIR,
    INSTALLED_INDEX,
    is_plain_name,
    powers_home,
    resolves_within,
)
from kiro_crew.atomic_write import atomic_write
from kiro_crew.platform_compat_files import rmtree_force
from kiro_crew.platform_compat_links import is_link_or_junction, unlink_link_or_junction
from kiro_crew.platform_lock_compat import file_lock
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.user_json import loads_user_json

#: The index key Kiro IDE and kiro-cli write.
INDEX_KEY = "installedPowers"
#: Serializes every writer of ``installed.json`` in this process tree.
LOCK_FILE = ".installed.json.lock"
#: Seconds one ``git clone`` may take.
CLONE_TIMEOUT_S = 300
MAX_URL_LEN = 2048
MAX_REF_LEN = 255
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
#: Environment variables that would point git at another repository or config.
_GIT_REDIRECT_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_TEMPLATE_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_ALLOW_PROTOCOL",
        "GIT_PROTOCOL_FROM_USER",
    }
)

# Machine-readable refusal codes; the dashboard branches on these.
CODE_BAD_SOURCE = "agent_plugin_bad_source"
CODE_COPY_FAILED = "agent_plugin_copy_failed"
CODE_CONVERSION_FAILED = "agent_plugin_conversion_failed"
CODE_INVALID = "agent_plugin_invalid"
CODE_NOT_A_PLUGIN = "agent_plugin_not_a_plugin"
CODE_ALREADY_INSTALLED = "agent_plugin_already_installed"
CODE_NOT_INSTALLED = "agent_plugin_not_installed"
CODE_INDEX_UNREADABLE = "agent_plugin_index_unreadable"
CODE_BAD_URL = "agent_plugin_bad_git_url"
CODE_BAD_REF = "agent_plugin_bad_git_ref"
CODE_BAD_SUBDIR = "agent_plugin_bad_subdir"
CODE_CLONE_FAILED = "agent_plugin_clone_failed"


class InstallError(Exception):
    """The install or uninstall was refused; ``code`` names why for a client."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class InstallResult:
    """What an install placed and what the loader says about it."""

    name: str
    #: The loader's record for ``installed/<name>/``; its state is ``off``.
    record: PluginRecord
    #: The conversion report, or ``None`` when the source was already standard.
    conversion: ConversionReport | None
    #: Copy notes for a standard source, such as links that were not copied.
    notes: tuple[str, ...] = ()
    #: True when an installed plugin of the same name was replaced.
    replaced: bool = False


def install_from_folder(
    src: str | os.PathLike[str], *, home: Path | None = None, replace: bool = False
) -> InstallResult:
    """Install the plugin in the folder *src*; it lands ``off``.

    Refused, with nothing under ``installed/`` or in ``installed.json`` changed,
    when the source is not a folder or overlaps ``installed/``, the conversion
    fails, the loader finds the result ``invalid`` or ``not-a-plugin``, or the
    manifest ``name`` is already installed and *replace* is false. With
    *replace* the installed folder is swapped for the new one; ``data/<name>/``
    is kept either way.
    """
    base = powers_home() if home is None else Path(home)
    installed = base / INSTALLED_DIR
    src_real = Path(os.path.realpath(src))
    if not src_real.is_dir():
        raise InstallError(CODE_BAD_SOURCE, "the source is not a folder")
    if _inside(src_real, installed) or _inside(installed, src_real):
        raise InstallError(CODE_BAD_SOURCE, "the source overlaps the installed plugins folder")

    created = _make_dirs(installed)
    stage: Path | None = None
    try:
        stage = Path(os.path.realpath(tempfile.mkdtemp(prefix=".installing-", dir=installed)))
        package = stage / "package"
        conversion, notes = _stage_package(src_real, package)
        name = _manifest_name(package)
        (stage / INSTALLED_DIR).mkdir()
        staged = stage / INSTALLED_DIR / name
        os.replace(package, staged)
        verdict = inspect_plugin(stage, name)
        if verdict.state != "off":
            code = CODE_NOT_A_PLUGIN if verdict.state == "not-a-plugin" else CODE_INVALID
            raise InstallError(code, verdict.reason or f"the plugin is {verdict.state}")
        with _index_lock(base):
            replaced = _place_and_list(base, name, staged, stage, replace)
    except BaseException as exc:
        if stage is not None:
            _discard(stage)
        for path in reversed(created):
            try:
                path.rmdir()
            except OSError:
                break
        if isinstance(exc, OSError):
            raise InstallError(
                CODE_COPY_FAILED, f"the plugin could not be written: {exc.__class__.__name__}"
            ) from exc
        raise
    _discard(stage)
    return InstallResult(
        name=name,
        record=inspect_plugin(base, name),
        conversion=conversion,
        notes=notes,
        replaced=replaced,
    )


def install_from_git(
    url: str,
    *,
    ref: str | None = None,
    subdir: str | None = None,
    home: Path | None = None,
    replace: bool = False,
) -> InstallResult:
    """Shallow-clone the ``https`` repository *url* and install from it.

    *ref* is a branch or tag; *subdir* names the plugin folder inside the
    repository and must stay inside the clone. The clone lives in a system
    temporary folder that is removed afterwards, runs no hook and checks
    symbolic links out as plain files.
    """
    url = validate_git_url(url)
    ref = _validate_ref(ref)
    rel = _validate_subdir(subdir)
    tmp = Path(tempfile.mkdtemp(prefix="kc-agent-plugin-clone-"))
    try:
        clone = tmp / "repo"
        try:
            proc = _run_clone(clone_argv(url, ref, clone), env=_clone_env(), cwd=str(tmp))
        except subprocess.TimeoutExpired as exc:
            raise InstallError(CODE_CLONE_FAILED, "git clone timed out") from exc
        except OSError as exc:
            raise InstallError(
                CODE_CLONE_FAILED, f"git could not start: {exc.__class__.__name__}"
            ) from exc
        if proc.returncode != 0:
            lines = [ln.strip() for ln in (proc.stderr or "").splitlines() if ln.strip()]
            detail = f": {lines[-1][:300]}" if lines else ""
            raise InstallError(
                CODE_CLONE_FAILED, f"git clone failed (exit {proc.returncode}){detail}"
            )
        return install_from_folder(_clone_subdir(clone, rel), home=home, replace=replace)
    finally:
        rmtree_force(tmp)


def uninstall(name: str, *, home: Path | None = None) -> None:
    """Remove ``installed/<name>/`` and its ``installed.json`` entries.

    ``data/<name>/`` is kept, so reinstalling the plugin finds its data again.
    Refused when *name* is neither listed nor present.
    """
    if not is_plain_name(name):
        raise InstallError(CODE_NOT_INSTALLED, "not a plugin name")
    base = powers_home() if home is None else Path(home)
    installed = base / INSTALLED_DIR
    target = installed / name
    if not base.is_dir():
        raise InstallError(CODE_NOT_INSTALLED, f"{name} is not installed")
    with _index_lock(base):
        data = _read_index(base)
        entries = data[INDEX_KEY]
        kept = [e for e in entries if not (isinstance(e, dict) and e.get("name") == name)]
        listed = len(kept) != len(entries)
        present = os.path.lexists(target)
        if not listed and not present:
            raise InstallError(CODE_NOT_INSTALLED, f"{name} is not installed")
        if listed:
            data[INDEX_KEY] = kept
            _write_index(base, data)
        if present:
            _remove_folder(target, installed)


def validate_git_url(url: object) -> str:
    """*url* when it is a plain ``https`` URL with a host and no credentials.

    ``file://``, ``ssh://``, scp-style ``git@host:path``, ``<transport>::``
    remote-helper addresses and local paths all fail the ``https://`` prefix.
    """
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LEN:
        raise InstallError(CODE_BAD_URL, "the git URL is missing or too long")
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        raise InstallError(CODE_BAD_URL, "the git URL contains whitespace or control characters")
    if not url.lower().startswith("https://"):
        raise InstallError(CODE_BAD_URL, "only https git URLs are accepted")
    parts = urlsplit(url)
    if "@" in parts.netloc:
        raise InstallError(CODE_BAD_URL, "the git URL must not carry credentials")
    try:
        port_ok = parts.port is None or 0 < parts.port
    except ValueError:
        port_ok = False
    if not parts.hostname or not port_ok:
        raise InstallError(CODE_BAD_URL, "the git URL has no valid host")
    if parts.fragment:
        raise InstallError(CODE_BAD_URL, "the git URL must not have a fragment")
    return url


def clone_argv(url: str, ref: str | None, dest: Path) -> list[str]:
    """The ``git clone`` command line for *url*: shallow, hook-free, https only."""
    argv = [
        "git",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        "-c",
        "core.symlinks=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.fsmonitor=false",
        "clone",
        "--depth",
        "1",
        "--no-recurse-submodules",
        "--template=",
    ]
    if ref:
        argv += ["--branch", ref]
    return argv + ["--", url, str(dest)]


def _run_clone(argv: list[str], *, env: dict[str, str], cwd: str) -> subprocess.CompletedProcess:
    """Run ``git clone``; the one spawn in this module, replaced in tests."""
    return subprocess.run(
        argv,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=CLONE_TIMEOUT_S,
        shell=False,
        check=False,
        **UTF8_TEXT,
    )


def _clone_env() -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _GIT_REDIRECT_ENV
        and not k.startswith("GIT_CONFIG_KEY_")
        and not k.startswith("GIT_CONFIG_VALUE_")
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _validate_ref(ref: object) -> str | None:
    if ref is None or ref == "":
        return None
    if (
        not isinstance(ref, str)
        or len(ref) > MAX_REF_LEN
        or not _REF_RE.match(ref)
        or ".." in ref
        or "//" in ref
        or ref.endswith(("/", ".", ".lock"))
    ):
        raise InstallError(CODE_BAD_REF, "the git ref is not a branch or tag name")
    return ref


def _validate_subdir(subdir: object) -> str | None:
    if subdir is None or subdir in ("", ".", "./"):
        return None
    if not isinstance(subdir, str) or "\x00" in subdir:
        raise InstallError(CODE_BAD_SUBDIR, "the folder inside the repository is not valid")
    rel = subdir.replace("\\", "/")
    parts = [p for p in PurePosixPath(rel).parts if p not in ("", ".")]
    if (
        PurePosixPath(rel).is_absolute()
        or PureWindowsPath(subdir).drive
        or rel.startswith("~")
        or ".." in parts
        or not parts
    ):
        raise InstallError(CODE_BAD_SUBDIR, "the folder must be a path inside the repository")
    return "/".join(parts)


def _clone_subdir(clone: Path, rel: str | None) -> Path:
    """*clone*/*rel*, reached through no link and resolving inside the clone."""
    clone_real = os.path.realpath(clone)
    path = Path(clone_real)
    for part in rel.split("/") if rel else ():
        path = path / part
        if is_link_or_junction(path):
            raise InstallError(CODE_BAD_SUBDIR, "the folder inside the repository is a link")
    if resolves_within(path, clone_real) is None or not path.is_dir():
        raise InstallError(CODE_BAD_SUBDIR, "the folder is not in the repository")
    return path


def _stage_package(src: Path, package: Path) -> tuple[ConversionReport | None, tuple[str, ...]]:
    """Build the plugin at *package*: a safe copy when *src* is standard, else a conversion.

    A folder with no client manifest is copied too, so the loader names it
    ``not-a-plugin``.
    """
    if find_client_manifest(src) is None:
        copy_report = ConversionReport(name="", source_manifest=MANIFEST_FILE)
        try:
            package.mkdir()
            _copy_package(src, package, copy_report)
        except ConversionError as exc:
            raise InstallError(CODE_COPY_FAILED, str(exc)) from exc
        return None, tuple(copy_report.notes)
    try:
        return convert_plugin(src, package), ()
    except ConversionError as exc:
        raise InstallError(CODE_CONVERSION_FAILED, str(exc)) from exc


def _manifest_name(package: Path) -> str:
    root_real = os.path.realpath(package)
    if not os.path.lexists(os.path.join(root_real, MANIFEST_FILE)):
        if os.path.lexists(os.path.join(root_real, LEGACY_POWER_FILE)):
            raise InstallError(CODE_NOT_A_PLUGIN, "legacy POWER.md power, not an Agent Plugin")
        raise InstallError(CODE_NOT_A_PLUGIN, "no plugin.json")
    try:
        manifest, _ = load_manifest(root_real)
    except ManifestError as exc:
        raise InstallError(CODE_INVALID, str(exc)) from exc
    return manifest.name


def _place_and_list(base: Path, name: str, staged: Path, stage: Path, replace: bool) -> bool:
    """Move *staged* to ``installed/<name>`` and list *name*; undo the move on failure.

    The caller holds the index lock. Returns True when an installed folder was
    replaced; the previous folder is left in *stage* for the caller to discard.
    """
    data = _read_index(base)
    entries = data[INDEX_KEY]
    listed = any(isinstance(e, dict) and e.get("name") == name for e in entries)
    target = base / INSTALLED_DIR / name
    exists = os.path.lexists(target)
    if (listed or exists) and not replace:
        raise InstallError(CODE_ALREADY_INSTALLED, f"{name} is already installed")
    previous = stage / "previous"
    if exists:
        os.replace(target, previous)
        try:
            os.replace(staged, target)
        except BaseException:
            os.replace(previous, target)
            raise
    else:
        os.replace(staged, target)
    try:
        if not listed:
            entries.append({"name": name})
            _write_index(base, data)
    except BaseException:
        os.replace(target, stage / "rejected")
        if exists:
            os.replace(previous, target)
        raise
    return exists


def _read_index(base: Path) -> dict[str, Any]:
    """``installed.json`` as written, with an ``installedPowers`` list.

    A missing index is an empty one. An index that cannot be read or parsed is
    refused rather than overwritten.
    """
    path = base / INSTALLED_INDEX
    try:
        if path.stat().st_size > MAX_INDEX_BYTES:
            raise InstallError(CODE_INDEX_UNREADABLE, "installed.json is too large to update")
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {INDEX_KEY: []}
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallError(
            CODE_INDEX_UNREADABLE, f"installed.json could not be read: {exc.__class__.__name__}"
        ) from exc
    try:
        data = loads_user_json(text)
    except (ValueError, RecursionError) as exc:
        raise InstallError(CODE_INDEX_UNREADABLE, "installed.json is not valid JSON") from exc
    if not isinstance(data, dict):
        raise InstallError(CODE_INDEX_UNREADABLE, "installed.json does not hold a JSON object")
    entries = data.setdefault(INDEX_KEY, [])
    if not isinstance(entries, list):
        raise InstallError(CODE_INDEX_UNREADABLE, f"{INDEX_KEY} in installed.json is not a list")
    return data


def _write_index(base: Path, data: dict[str, Any]) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    atomic_write(base / INSTALLED_INDEX, text, newline="")


@contextlib.contextmanager
def _index_lock(base: Path) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(base / LOCK_FILE, flags, 0o600)
    try:
        with file_lock(fd):
            yield
    finally:
        os.close(fd)


def _remove_folder(target: Path, installed: Path) -> None:
    """Take *target* out of ``installed/`` at once, then delete it."""
    if is_link_or_junction(target):
        unlink_link_or_junction(target)
        return
    trash = Path(tempfile.mkdtemp(prefix=".removing-", dir=installed))
    os.replace(target, trash / "plugin")
    _discard(trash)


def _discard(folder: Path) -> None:
    """Delete a staging folder; a link inside it is removed, never followed."""
    for child in ("previous", "rejected"):
        path = folder / child
        if is_link_or_junction(path):
            unlink_link_or_junction(path)
    rmtree_force(folder)


def _make_dirs(path: Path) -> list[Path]:
    """Create *path* and its missing parents; return those created, outermost first."""
    missing: list[Path] = []
    cur = path
    while not os.path.lexists(cur):
        missing.append(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    created: list[Path] = []
    try:
        for p in reversed(missing):
            p.mkdir()
            created.append(p)
    except OSError as exc:
        for p in reversed(created):
            try:
                p.rmdir()
            except OSError:
                break
        raise InstallError(
            CODE_COPY_FAILED,
            f"the installed plugins folder could not be created: {exc.__class__.__name__}",
        ) from exc
    return created


def _inside(path: Path, root: Path) -> bool:
    try:
        real_root = os.path.realpath(root)
        return os.path.commonpath([os.path.realpath(path), real_root]) == real_root
    except ValueError:
        return False
