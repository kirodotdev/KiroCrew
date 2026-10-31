"""Which language server handles a file, how it is launched, and where its project root is."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Placeholder in a launch argv for the per-root scratch directory a server keeps
# its index in (jdtls's ``-data``). Substituted by :func:`resolve_argv`.
DATA_DIR_PLACEHOLDER = "{data_dir}"


@dataclass(frozen=True)
class ServerSpec:
    """One language server family.

    ``commands`` are tried in order; the first whose executable is on ``$PATH``
    and passes the provenance check is launched. ``languages`` maps a lowercase
    file extension to the LSP ``languageId`` sent in ``didOpen``.
    """

    id: str
    languages: dict[str, str]
    commands: tuple[tuple[str, ...], ...]
    root_markers: tuple[str, ...]


SERVER_SPECS: tuple[ServerSpec, ...] = (
    ServerSpec(
        id="python",
        languages={".py": "python", ".pyi": "python"},
        commands=(
            ("pyright-langserver", "--stdio"),
            ("basedpyright-langserver", "--stdio"),
            ("pylsp",),
            ("jedi-language-server",),
        ),
        root_markers=("pyproject.toml", "setup.py", "setup.cfg", "pyrightconfig.json"),
    ),
    ServerSpec(
        id="typescript",
        languages={
            ".ts": "typescript",
            ".tsx": "typescriptreact",
            ".mts": "typescript",
            ".cts": "typescript",
            ".js": "javascript",
            ".jsx": "javascriptreact",
            ".mjs": "javascript",
            ".cjs": "javascript",
        },
        commands=(("typescript-language-server", "--stdio"),),
        root_markers=("tsconfig.json", "jsconfig.json", "package.json"),
    ),
    ServerSpec(
        id="go",
        languages={".go": "go"},
        commands=(("gopls",),),
        root_markers=("go.work", "go.mod"),
    ),
    ServerSpec(
        id="rust",
        languages={".rs": "rust"},
        commands=(("rust-analyzer",),),
        root_markers=("Cargo.toml",),
    ),
    ServerSpec(
        id="java",
        languages={".java": "java"},
        commands=(("jdtls", "-data", DATA_DIR_PLACEHOLDER),),
        root_markers=(
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            ".classpath",
        ),
    ),
    ServerSpec(
        id="kotlin",
        languages={".kt": "kotlin", ".kts": "kotlin"},
        commands=(("kotlin-language-server",),),
        root_markers=("build.gradle.kts", "build.gradle", "settings.gradle.kts", "pom.xml"),
    ),
    ServerSpec(
        id="c",
        languages={
            ".c": "c",
            ".h": "c",
            ".cc": "cpp",
            ".cpp": "cpp",
            ".cxx": "cpp",
            ".hpp": "cpp",
            ".hh": "cpp",
        },
        commands=(("clangd",),),
        root_markers=("compile_commands.json", "compile_flags.txt", "CMakeLists.txt"),
    ),
    ServerSpec(
        id="ruby",
        languages={".rb": "ruby"},
        commands=(("ruby-lsp",), ("solargraph", "stdio")),
        root_markers=("Gemfile",),
    ),
)

# A root marker every family accepts, consulted only after the family's own markers.
_FALLBACK_ROOT_MARKERS = (".git",)


def spec_for_path(path: str) -> ServerSpec | None:
    """The server family for *path* by extension, or None."""
    ext = os.path.splitext(path)[1].lower()
    for spec in SERVER_SPECS:
        if ext in spec.languages:
            return spec
    return None


def language_id(spec: ServerSpec, path: str) -> str:
    return spec.languages.get(os.path.splitext(path)[1].lower(), spec.id)


def find_project_root(path: str, spec: ServerSpec) -> str:
    """The directory the server is started in for *path*.

    The nearest ancestor holding one of the family's markers wins; failing that,
    the nearest repository root; failing that, the file's own directory. The
    walk up stops short of the home directory and the filesystem root, so a
    marker there never claims every project beneath it; a file that sits
    directly in one of them still roots there, and the LSP tab, where every
    server is started, shows that root before the user starts it.

    Blocking (stats each ancestor).
    """
    start = os.path.dirname(os.path.realpath(path))
    home = os.path.realpath(os.path.expanduser("~"))
    for markers in (spec.root_markers, _FALLBACK_ROOT_MARKERS):
        current = start
        while True:
            if current in (home, os.path.dirname(current)):
                break
            if any(os.path.exists(os.path.join(current, m)) for m in markers):
                return current
            current = os.path.dirname(current)
    return start


def spec_by_id(server_id: str) -> ServerSpec | None:
    """The server family named *server_id*, or None."""
    for spec in SERVER_SPECS:
        if spec.id == server_id:
            return spec
    return None


def _uses_eclipse_files(root: str) -> bool:
    """Whether *root* carries Eclipse project files (bemol writes them). Blocking."""
    return all(os.path.isfile(os.path.join(root, name)) for name in (".project", ".classpath"))


# Cap on the Eclipse projects one jdtls opens, following references transitively.
MAX_PROJECT_FOLDERS = 64
# Bytes of a .project / .classpath read; the files bemol writes are far smaller.
_ECLIPSE_FILE_BYTES = 1024 * 1024
_PROJECT_NAME = re.compile(r"<projectDescription>\s*<name>\s*([^<]+?)\s*</name>", re.S)
_PROJECT_REFERENCE = re.compile(
    r"<classpathentry\b[^>]*\bkind=\"src\"[^>]*\bpath=\"/([^\"/]+)\"|"
    r"<classpathentry\b[^>]*\bpath=\"/([^\"/]+)\"[^>]*\bkind=\"src\""
)


def _read_head(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(_ECLIPSE_FILE_BYTES)
    except OSError:
        return ""


def _eclipse_name(folder: str) -> str:
    match = _PROJECT_NAME.search(_read_head(os.path.join(folder, ".project")))
    return match.group(1) if match else ""


def _project_references(folder: str) -> list[str]:
    """Names of the Eclipse projects *folder*'s ``.classpath`` depends on, in order.

    A ``src`` entry whose path is ``/Name`` names another project, not a folder:
    it resolves only when a project called Name is open in the same workspace.
    """
    names: list[str] = []
    for match in _PROJECT_REFERENCE.finditer(_read_head(os.path.join(folder, ".classpath"))):
        name = match.group(1) or match.group(2)
        if name not in names:
            names.append(name)
    return names


def project_folders(spec: ServerSpec, root: str) -> tuple[list[str], list[str]]:
    """The folders one server at *root* should open, and the references not found.

    jdtls resolves a project reference only against projects open in the same
    instance, so *root* is opened together with every project its ``.classpath``
    references, transitively. A reference (``path="/Name"``) names a project, not
    a folder: Eclipse maps the name to a folder through its workspace, which is
    not available here, so the project is looked for among the sibling folders
    of the one that references it, by the ``<name>`` in its ``.project``. The
    folder named like the project is tried first, which is the usual layout and
    reads one file; only when that is not the project are the other siblings
    listed. Other servers, and roots without Eclipse files, open *root* alone.
    Blocking (reads files).
    """
    if spec.id != "java" or not _uses_eclipse_files(root):
        return [root], []
    from kiro_crew.security.paths import is_sensitive_resolved_path

    by_parent: dict[str, dict[str, str]] = {}

    def same_named(parent: str, name: str) -> str | None:
        """The sibling folder called *name*, when it is the project called *name*."""
        if not name or "/" in name or name.startswith("."):
            return None
        folder = os.path.realpath(os.path.join(parent, name))
        if (
            os.path.dirname(folder) != os.path.realpath(parent)
            or is_sensitive_resolved_path(folder)
            or not _uses_eclipse_files(folder)
        ):
            return None
        return folder if _eclipse_name(folder) == name else None

    def siblings(parent: str) -> dict[str, str]:
        if parent not in by_parent:
            found: dict[str, str] = {}
            try:
                entries = sorted(os.scandir(parent), key=lambda e: e.name)
            except OSError:
                entries = []
            for entry in entries:
                if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=True):
                    continue
                folder = os.path.realpath(entry.path)
                if is_sensitive_resolved_path(folder) or not _uses_eclipse_files(folder):
                    continue
                name = _eclipse_name(folder)
                if name:
                    found.setdefault(name, folder)
            by_parent[parent] = found
        return by_parent[parent]

    folders = [root]
    missing: list[str] = []
    queue = [root]
    while queue and len(folders) < MAX_PROJECT_FOLDERS:
        folder = queue.pop(0)
        for name in _project_references(folder):
            parent = os.path.dirname(folder)
            target = same_named(parent, name) or siblings(parent).get(name)
            if target is None:
                if name not in missing:
                    missing.append(name)
            elif target not in folders and len(folders) < MAX_PROJECT_FOLDERS:
                folders.append(target)
                queue.append(target)
    return folders, missing


def data_dir_for(spec: ServerSpec, root: str, *, create: bool = True) -> str:
    """A per-(server, root, import mode) scratch directory under the user cache.

    jdtls keeps the project model it built in this directory and reloads it on the
    next start. A root that gains Eclipse project files after a failed Gradle
    import would otherwise reopen the failed project, so the mode is part of the
    key and the switch starts from an empty workspace.
    """
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    key = root
    if spec.id == "java" and _uses_eclipse_files(root):
        key = root + "\0eclipse"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    path = os.path.join(base, "kirocrew", "code-intel", spec.id, digest)
    if create:
        os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def resolve_argv(
    spec: ServerSpec, root: str, refusals: list[str] | None = None, *, create: bool = True
) -> list[str] | None:
    """The first launchable command for *spec*, as an argv with an absolute executable.

    Each candidate goes through :func:`validate_provider_executable`, the repo's
    executable-provenance check: a server binary planted in the project checkout
    would otherwise run with the gateway's environment the moment the user opens
    a file. Each refused candidate's reason is appended to *refusals*. With
    *create* false the server's data directory is named but not made, for a
    caller that only reports whether a server could start.
    Blocking (``which`` and the provenance walk stat).
    """
    from kiro_crew.github_runner import validate_provider_executable

    for command in spec.commands:
        found = shutil.which(command[0])
        if not found:
            continue
        try:
            executable = validate_provider_executable(found)
        except ValueError as exc:
            logger.warning("code_intel: refusing %s at %s: %s", command[0], found, exc)
            if refusals is not None:
                refusals.append(f"{found}: {exc}")
            continue
        argv = [executable, *command[1:]]
        if DATA_DIR_PLACEHOLDER in argv:
            data_dir = data_dir_for(spec, root, create=create)
            argv = [data_dir if arg == DATA_DIR_PLACEHOLDER else arg for arg in argv]
        return argv
    return None


def initialization_options(spec: ServerSpec, root: str, folders: list[str] | None = None) -> dict:
    """``initializationOptions`` for *spec* started at *root*. Blocking (stats *root*).

    jdtls: a root that already carries Eclipse project files (``.project`` and
    ``.classpath``, as bemol writes for every package of a Brazil workspace) is
    opened through them, with the Gradle and Maven importers off. Otherwise jdtls
    prefers ``build.gradle`` and runs the build to learn the classpath, and a
    Brazil build fails without a live Midway session and leaves no project, so
    every answer comes back empty.

    jdtls imports the folders named in ``initializationOptions.workspaceFolders``,
    not the LSP ``workspaceFolders``, so *folders* (the root and the projects it
    references, from :func:`project_folders`) are passed there too.
    """
    if spec.id != "java":
        return {}
    # Library classes come back as jdt:// URIs whose source the client fetches
    # with java/classFileContents (see classfiles.py).
    options: dict = {"extendedClientCapabilities": {"classFileContentsSupport": True}}
    if folders and len(folders) > 1:
        from kiro_crew.apps.builtins.code_intel.protocol import path_to_uri

        options["workspaceFolders"] = [path_to_uri(f) for f in folders]
    if _uses_eclipse_files(root):
        options["settings"] = {
            "java": {"import": {"gradle": {"enabled": False}, "maven": {"enabled": False}}}
        }
    return options


def missing_references_hint(missing: list[str]) -> str:
    """The setup line for project references no sibling folder provides, or ''."""
    if not missing:
        return ""
    shown = ", ".join(missing[:8]) + (f" and {len(missing) - 8} more" if len(missing) > 8 else "")
    return (
        f"The project depends on Eclipse projects not found beside it: {shown}. "
        "Symbols from them will not resolve until they are checked out there."
    )


def setup_hint(spec: ServerSpec, root: str) -> str:
    """A one-line reason the server will not see the project, or ''. Blocking (stats).

    jdtls in a Brazil package with no Eclipse project files falls back to the
    Gradle import, which fails without a live Midway session: bemol, run at the
    workspace root, writes those files for every package.
    """
    if spec.id != "java":
        return ""
    if os.path.isfile(os.path.join(root, ".classpath")):
        # A Gradle import that ran over bemol's files (jdtls before the Eclipse
        # importer was preferred) rewrites .project without the Java nature, and
        # jdtls then treats the package as plain files: outlines work, nothing
        # else resolves.
        try:
            with open(os.path.join(root, ".project"), encoding="utf-8") as fh:
                has_nature = "org.eclipse.jdt.core.javanature" in fh.read(256 * 1024)
        except OSError:
            has_nature = False
        if has_nature:
            return _missing_libraries_hint(root)
        workspace = _brazil_workspace(root)
        where = f" in {workspace}" if workspace else ""
        return (
            f"{root}/.project has no Java nature, so the language server sees plain "
            f"files and cannot resolve symbols. Re-run bemol{where}, then restart "
            "this server."
        )
    workspace = _brazil_workspace(root)
    if workspace:
        return (
            f"No Eclipse project files in {root}. Run bemol in {workspace} to "
            "write them; without them the language server imports the Gradle "
            "build, which needs a Midway session."
        )
    return ""


_LIBRARY_ENTRY = re.compile(r'<classpathentry\b[^>]*\bkind="lib"[^>]*\bpath="([^"]+)"')
# Library entries stat-checked per root; a classpath this long is checked in part.
_MAX_LIBRARIES_CHECKED = 2000


def _missing_libraries_hint(root: str) -> str:
    """The setup line when ``.classpath`` lists jars that are not on disk, or ''.

    Eclipse files written on another machine (a laptop's ``/Volumes`` mount, say)
    or before the package cache was cleaned name jars that are not there. jdtls
    then resolves none of their classes, and a name such as ``Component`` can
    resolve to an unrelated JDK class instead. Blocking (stats each jar).
    """
    jars = _LIBRARY_ENTRY.findall(_read_head(os.path.join(root, ".classpath")))
    jars = [j for j in jars if os.path.isabs(j)][:_MAX_LIBRARIES_CHECKED]
    missing = [j for j in jars if not os.path.exists(j)]
    if not missing:
        return ""
    workspace = _brazil_workspace(root)
    where = f" in {workspace}" if workspace else ""
    return (
        f"{len(missing)} of {len(jars)} libraries in {root}/.classpath are not on disk "
        f"(for example {missing[0]}), so their classes will not resolve. Re-generate "
        f"the Eclipse files{where} (bemol does it for packages in packageInfo), then "
        "restart this server."
    )


def _brazil_workspace(root: str) -> str:
    """The nearest ancestor of *root* holding ``packageInfo``, or ''. Blocking."""
    current = root
    while current != os.path.dirname(current):
        if os.path.isfile(os.path.join(current, "packageInfo")):
            return current
        current = os.path.dirname(current)
    return ""


def install_hint(spec: ServerSpec) -> list[str]:
    """The executable names that would serve *spec*, for the "no server" answer."""
    return [command[0] for command in spec.commands]
