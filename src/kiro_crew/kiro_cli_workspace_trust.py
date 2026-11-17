"""Read kiro-cli's own "Trust this workspace?" answer for a folder. Read-only.

kiro-cli (V3) stores the answer as ``workspaceTrusted: true`` in the per-workspace
policy file under the user's ``.kiro``:
``<kiro home>/workspace-roots/<bucket>/permissions.yaml`` (or ``.json``). This
module is a port of the reader in kiro-cli's ``packages/tui-common/src/workspace``
(``workspace-home.ts``, ``workspace-hash.ts``, ``workspace-policy-file.ts``,
``workspace-trust-store.ts`` ``readWorkspaceTrust``), so the folder Crew asks about
lands in the same bucket kiro-cli wrote, Windows case rules included.

Every answer other than a readable record carrying ``workspaceTrusted: true`` in the
folder's own case mode is "not trusted": a missing, unreadable, non-UTF-8, blank or
unparsable file, a folder whose identity cannot be settled, and the user's home
(kiro-cli never reads a grant for home). Nothing here raises or writes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import ntpath
import os
import posixpath
import sys
from typing import Any

logger = logging.getLogger(__name__)

_WORKSPACE_ROOTS_DIRNAME = "workspace-roots"
_POLICY_FILENAMES = ("permissions.yaml", "permissions.json")
_TRUSTED_KEY = "workspaceTrusted"
_CASE_KEY = "workspaceTrustCase"
#: The separator kiro-cli normalizes every workspace path to before hashing. A string
#: rule of the hash, not a filesystem separator.
_SLASH = "/"


def _is_windows() -> bool:
    return sys.platform == "win32"


def _collapse_posix(path: str) -> str:
    """kiro-cli's ``collapsePosix``: ``//``, ``.`` and ``..`` collapsed, trailing ``/`` kept."""
    if not path:
        return "."
    rooted = path.startswith("/")
    trailing = path.endswith("/")
    parts: list[str] = []
    for segment in path.split(_SLASH):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            elif not rooted:
                parts.append("..")
            continue
        parts.append(segment)
    joined = _SLASH.join(parts)
    if not joined:
        if rooted:
            return "/"
        return "./" if trailing else "."
    body = f"{joined}/" if trailing else joined
    return f"/{body}" if rooted else body


def _normalize(path: str, *, fold: bool) -> str:
    """kiro-cli's ``normalizeWorkspacePathCase``."""
    absolute = path if os.path.isabs(path) else os.path.abspath(path)
    forward = absolute.replace("\\", "/")
    is_unc = _is_windows() and forward.startswith("//")
    canonical = _collapse_posix(forward)
    if is_unc and not canonical.startswith("//"):
        canonical = f"/{canonical}"
    if len(canonical) > 1:
        stripped = canonical.rstrip("/")
        bare_drive = (
            len(stripped) == 2
            and stripped[0].isascii()
            and stripped[0].isalpha()
            and stripped[1] == ":"
        )
        if stripped and not bare_drive:
            canonical = stripped
    return canonical.lower() if fold else canonical


def _stat_id(path: str) -> tuple[int, int] | None:
    """``(dev, ino)``, or ``None`` when unreadable or the file id is zero."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if st.st_ino == 0:
        return None
    return st.st_dev, st.st_ino


def _windows_case_mode(physical: str) -> str | None:
    """kiro-cli's ``windowsCaseMode``: ``exact``, ``folded``, or ``None`` (unsettled)."""
    prefix = ntpath.splitdrive(physical)[0]
    rest = physical[len(prefix) :]
    if rest[:1] in ("\\", "/"):
        prefix += rest[0]
    names = [n for n in physical[len(prefix) :].replace("\\", _SLASH).split(_SLASH) if n]
    parent = prefix
    mode = "folded"
    for name in names:
        lower = name.lower()
        other = name.upper() if lower == name else lower
        path = ntpath.join(parent, name)
        if other != name:
            first = _stat_id(path)
            if first is None:
                return None
            try:
                st = os.stat(ntpath.join(parent, other))
            except FileNotFoundError:
                mode = "exact"
            except NotADirectoryError:
                mode = "exact"
            except OSError:
                return None
            else:
                if st.st_ino == 0:
                    return None
                if (st.st_dev, st.st_ino) != first:
                    mode = "exact"
        parent = path
    return mode


def _identity(cwd: str) -> tuple[str, str] | None:
    """kiro-cli's ``resolveWorkspaceTrustIdentity`` as ``(root, case_mode)``, or ``None``."""
    try:
        physical = os.path.realpath(cwd, strict=True)
    except (OSError, ValueError):
        return None
    if _is_windows():
        if physical[:8].upper() == "\\\\?\\UNC\\":
            physical = "\\\\" + physical[8:]  # \\?\UNC\server\share -> \\server\share
        elif physical.startswith("\\\\?\\"):
            physical = physical[4:]
        case_mode = _windows_case_mode(physical)
        if case_mode is None:
            return None
    else:
        case_mode = "folded"
    if case_mode == "exact":
        kept = _normalize(physical, fold=False)
        drive = ntpath.splitdrive(physical)[0]
        cut = len(drive.replace("\\", "/")) + (
            1 if physical[len(drive) : len(drive) + 1] in ("\\", "/") else 0
        )
        root = kept[:cut].lower() + kept[cut:]
    else:
        root = _normalize(physical, fold=_is_windows())
    a, b = _stat_id(cwd), _stat_id(root)
    if a is None or a != b:
        return None
    return root, case_mode


def _os_home() -> str:
    """kiro-cli's ``policyOsHome``: a non-empty ``USERPROFILE``/``HOME``, else the lookup."""
    value = os.environ.get("USERPROFILE" if _is_windows() else "HOME")
    return value or os.path.expanduser("~")


def _real_prefix(paths: Any, path: str) -> str:
    tail: list[str] = []
    current = path
    while True:
        try:
            return paths.join(os.path.realpath(current, strict=True), *reversed(tail))
        except (OSError, ValueError):
            parent = paths.dirname(current)
            if parent == current:
                return path
            tail.append(paths.basename(current))
            current = parent


def _kiro_home() -> str:
    """kiro-cli's ``workspacePolicyHomeFrom``, without the test-only redirect.

    A release host never honours ``KIRO_TEST_POLICY_HOME``, so neither does Crew.
    """
    paths = ntpath if _is_windows() else posixpath
    home = _os_home()
    fallback = paths.join(home, ".kiro")
    override = (os.environ.get("KIRO_HOME") or "").strip()
    if not override or not paths.isabs(override):
        return fallback
    real = _real_prefix(paths, paths.abspath(override))
    if paths.dirname(real) == real:  # a filesystem root
        return fallback
    try:
        rel = paths.relpath(_real_prefix(paths, paths.abspath(home)), real)
    except ValueError:  # different drives on Windows
        return paths.abspath(override)
    contains_home = rel == "." or (not rel.startswith("..") and not paths.isabs(rel))
    return fallback if contains_home else paths.abspath(override)


def _bucket(root: str, case_mode: str) -> str:
    """kiro-cli's ``workspacePolicyDir`` bucket name."""
    if case_mode == "exact":
        text = f"case-exact\0{root}"
    else:
        text = _normalize(root, fold=_is_windows())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


#: A kiro-cli policy file is a few rules and one flag; anything larger is refused.
_MAX_POLICY_BYTES = 64 * 1024


def _read_text(path: str) -> str | None:
    """The file's UTF-8 text; ``None`` when missing. Raises on any other failure.

    The file sits where a sandboxed agent can write, so it is read the way
    ``spec_hooks._hook_file_bytes`` reads ``~/.kiro/hooks/*.json``: through
    ``safe_read_file_bytes_nolink``, which opens non-blocking, refuses a link at the
    name, a hardlinked inode and anything but a regular file (a FIFO or device
    never blocks or streams), and caps the size. A refused file raises rather than
    reading as missing, so it counts as present and reads as "not trusted".
    """
    import stat

    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

    try:
        named = os.lstat(path)
    except FileNotFoundError:
        return None
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(named.st_mode) or (getattr(named, "st_file_attributes", 0) & reparse):
        # The reader resolves a link at the name before it opens; kiro-cli writes a
        # plain file, so a link is refused outright. A link swapped in after this
        # check still meets the reader's regular-file, size and sensitive-path gates.
        raise OSError(f"refusing to read {os.path.basename(path)}: it is a link")
    try:
        data = safe_read_file_bytes_nolink(path, max_bytes=_MAX_POLICY_BYTES)
    except FileTooLargeError as exc:
        raise OSError(f"refusing to read {os.path.basename(path)}: {exc}") from None
    if data is None:
        if not os.path.lexists(path):
            return None
        raise OSError(f"refusing to read {os.path.basename(path)}: not a plain regular file")
    return data.decode("utf-8")  # strict, BOM kept, as kiro-cli reads it


def _policy_path(directory: str) -> str:
    """kiro-cli's ``workspaceTrustPath``: first non-blank file (yaml first), else yaml."""
    for name in _POLICY_FILENAMES:
        path = os.path.join(directory, name)
        try:
            text = _read_text(path)
        except (OSError, UnicodeDecodeError):
            return path  # unreadable counts as populated, and reads as corrupt
        if text is not None and text.strip():
            return path
    return os.path.join(directory, _POLICY_FILENAMES[0])


#: The YAML 1.2 spellings of true, as kiro-cli's parser reads a plain scalar.
_YAML_TRUE = ("true", "True", "TRUE")
_BOOL_TAG = "tag:yaml.org,2002:bool"
_STR_TAG = "tag:yaml.org,2002:str"


def _is_yaml_true(node: Any) -> bool:
    """A plain scalar with the bool tag, spelled as one of YAML 1.2's trues."""
    return node.style is None and node.tag == _BOOL_TAG and node.value in _YAML_TRUE


def _parse_yaml(text: str) -> dict[str, Any]:
    """The top-level scalars of a YAML policy file, read from the composed node tree.

    Only a plain (unquoted) ``true``/``True``/``TRUE`` reads as ``True``, YAML 1.2's
    booleans; every other scalar keeps its literal text, so ``yes`` or ``'true'`` is
    not a grant. Nothing is constructed, so no tag runs.

    The top-level mapping is refused whole (raises, so the record reads as not
    trusted) when a key is anything but a plain scalar that resolves to a string, or
    when a key repeats. kiro-cli writes only such keys, so this never refuses a record
    it wrote, and no rule of YAML key equality has to be matched. Nested mappings are
    never read, so they are not checked.
    """
    import yaml  # type: ignore[import-untyped]

    loader = yaml.SafeLoader(text)
    try:
        root = loader.get_single_node()
    finally:
        loader.dispose()
    if not isinstance(root, yaml.MappingNode):
        raise ValueError("policy file is not a mapping")
    doc: dict[str, Any] = {}
    for key_node, value_node in root.value:
        plain_str = (
            isinstance(key_node, yaml.ScalarNode)
            and key_node.style is None
            and key_node.tag == _STR_TAG
        )
        if not plain_str:
            raise ValueError("policy file key is not a plain string")
        if key_node.value in doc:
            raise ValueError("duplicate key in policy file")
        value: Any = value_node
        if isinstance(value_node, yaml.ScalarNode):
            value = True if _is_yaml_true(value_node) else value_node.value
        doc[key_node.value] = value
    return doc


def _parse(path: str, text: str) -> dict[str, Any]:
    doc = json.loads(text) if path.endswith(".json") else _parse_yaml(text)
    if not isinstance(doc, dict):
        raise ValueError("policy file is not a mapping")
    return doc


def kiro_cli_trusts_folder(folder: str | os.PathLike[str] | None) -> bool:
    """Whether kiro-cli recorded "trust this workspace" for *folder*. Never raises."""
    if not folder:
        return False
    try:
        identity = _identity(os.fspath(folder))
        if identity is None:
            return False
        root, case_mode = identity
        home = _identity(_os_home())
        if home is not None and home[0] == root:
            return False  # kiro-cli never reads a grant for home
        path = _policy_path(
            os.path.join(_kiro_home(), _WORKSPACE_ROOTS_DIRNAME, _bucket(root, case_mode))
        )
        text = _read_text(path)
        if text is None or not text.strip():
            return False
        doc = _parse(path, text)
    except Exception:  # noqa: BLE001 - any failure is "not trusted", silently
        logger.debug("kiro-cli workspace trust for %s could not be read", folder, exc_info=True)
        return False
    stored_case = doc.get(_CASE_KEY)
    case_ok = stored_case == "exact" if case_mode == "exact" else _CASE_KEY not in doc
    return doc.get(_TRUSTED_KEY) is True and case_ok
