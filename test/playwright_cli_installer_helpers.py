"""Stubs, environments and constants shared by the Playwright CLI installer tests.

``test_playwright_cli_installer.py`` and its ``_contract``, ``_install`` and ``_url_redaction``
siblings import these by name: the two installer paths, the exit-code table, the tool stubs
(``node``, ``npm``, ``curl``) on a PATH the test controls, the hermetic and isolated
environments, the native-tool lookup that skips version-manager shims, the bounded installer
run, and the fake Node mirror with the tarballs the bootstrap verifies.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from installer_test_helpers import run_bounded

#: The Node version the bootstrap fixtures build tarballs for. Asserted equal to
#: both scripts' own default, so a bump cannot leave these tests exercising a
#: version the installer does not ship.
TESTED_NODE_VERSION = "22.23.2"

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_SH = REPO_ROOT / "playwright-cli.sh"
INSTALLER_PS1 = REPO_ROOT / "playwright-cli.ps1"

#: Mirrors the exit-code table in both installers' usage text. A test asserts the
#: scripts still agree with this map, so a renumbering cannot land silently.
EXIT_CODES = {
    "usage": 2,
    "missing_tool": 10,
    "node_bootstrap": 11,
    "checksum": 12,
    "registry_auth": 13,
    "registry_unreachable": 14,
    "package_not_found": 15,
    "browser_download": 16,
    "not_writable": 17,
    "verify": 18,
}

#: Utilities the installer calls out to. The isolated-PATH scenarios symlink
#: exactly these, so a scenario expresses "curl is absent" by absence rather than
#: by shadowing a real binary (`command -v` skips a non-executable shadow and
#: finds the host's binary beneath it).
BASE_UTILS = (
    "sh",
    "env",
    "uname",
    "awk",
    "sed",
    "grep",
    "sort",
    "head",
    "tail",
    "tr",
    "cat",
    "printf",
    "mkdir",
    "mv",
    "rm",
    "chmod",
    "dirname",
    "basename",
    "mktemp",
    "tar",
    "gzip",
    "sha256sum",
    "getconf",
    "ln",
    "cut",
    "id",
    "date",
)

#: Applied per test rather than to the module, because only the tests that RUN
#: the POSIX script are POSIX-only. The rest read the files, and the PowerShell
#: parse test in particular must be allowed to run on the Windows shards -- that
#: is the one runner where a PowerShell interpreter is always present.
posix_only = pytest.mark.skipif(
    os.name == "nt", reason="playwright-cli.sh is POSIX shell (macOS + Linux)"
)


def _write_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _fake_node(stubs: Path) -> None:
    """A `node` that answers only the three probes the installer makes.

    The version probes decide whether the installer accepts this toolchain; the
    `-e` branch is the installed-version lookup, which the test steers with
    ``PWCLI_FAKE_INSTALLED_VERSION`` so an "already installed" scenario needs no
    real node_modules tree on disk.
    """
    _write_stub(
        stubs / "node",
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"process.execPath"*) printf "%s\\n" "$0" ;;\n'
        '  *"process.versions.node.split"*) echo "${PWCLI_FAKE_NODE_MAJOR:-22}" ;;\n'
        '  *"process.versions.node"*) echo "${PWCLI_FAKE_NODE_MAJOR:-22}.0.0" ;;\n'
        '  -e*) printf "%s" "${PWCLI_FAKE_INSTALLED_VERSION:-}" ;;\n'
        '  *"playwright-cli.js"*) exec "$@" ;;\n'
        "  *) : ;;\n"
        "esac\n"
        "exit 0\n",
    )


def _fake_npm_failing(stubs: Path, output: str) -> None:
    """An `npm` that reproduces a real failure's output and exits non-zero."""
    _write_stub(
        stubs / "npm",
        "#!/bin/sh\n" f"cat <<'NPMEOF' >&2\n{output}\nNPMEOF\n" "exit 1\n",
    )


def _fake_npm_succeeding(
    stubs: Path, *, version: str = "0.1.18", browser_failure: str | None = None
) -> None:
    """An `npm` that does what a real `--global` install leaves behind.

    It honors ``npm_config_prefix`` exactly as npm does, so the installer's
    wrapper generation and verification run against a realistic layout.

    ``browser_failure`` makes the installed CLI fail `install-browser` with that
    text, which is the only way to reach the installer's blocked-CDN branch.
    """
    if browser_failure is None:
        browser_step = (
            '  printf "%s\\n" "${PLAYWRIGHT_DOWNLOAD_HOST:-unset}" \\\n'
            '    > "$(dirname "$0")/../../../../install-browser-called"\n'
            "  exit 0\n"
        )
    else:
        browser_step = f'  echo "{browser_failure}" >&2\n  exit 1\n'
    _write_stub(
        stubs / "npm",
        "#!/bin/sh\n"
        # Real npm refuses to load one path as two config scopes and exits before
        # resolving anything. The stub enforces that rule, because a stub that
        # merely records the env vars let a broken --isolated-npmrc pass 75 tests.
        'if [ -n "${npm_config_userconfig:-}" ] '
        '&& [ "${npm_config_userconfig:-}" = "${npm_config_globalconfig:-}" ]; then\n'
        '  echo "npm error Exit prior to config file resolving" >&2\n'
        '  echo "npm error double-loading config \\"$npm_config_userconfig\\" as '
        '\\"global\\", previously loaded as \\"user\\"" >&2\n'
        "  exit 1\n"
        "fi\n"
        'mkdir -p "$npm_config_prefix/bin" '
        '"$npm_config_prefix/lib/node_modules/@playwright/cli"\n'
        # QUOTED delimiter, and the marker path is resolved from $0 at RUN time
        # rather than interpolated at write time. With an unquoted `<<EOF` the
        # shell expanded `$npm_config_prefix` into the generated script as live
        # source, so a prefix containing `$(...)` -- which
        # `test_wrapper_generation_escapes_the_paths_it_embeds` deliberately
        # supplies -- executed when the installer ran this stub. That made the
        # one test guarding the installer's path escaping inject from its own
        # fixture instead, and it wrote its marker into the process CWD where
        # that test's `tmp_path` assertion could not see it.
        "cat > \"$npm_config_prefix/lib/node_modules/@playwright/cli/playwright-cli.js\" <<'EOF'\n"
        "#!/bin/sh\n"
        f'[ "$1" = "--version" ] && echo "{version}" && exit 0\n'
        'if [ "$1" = "install-browser" ]; then\n'
        f"{browser_step}"
        "fi\n"
        "exit 0\n"
        "EOF\n"
        'chmod 755 "$npm_config_prefix/lib/node_modules/@playwright/cli/playwright-cli.js"\n'
        'ln -sf "../lib/node_modules/@playwright/cli/playwright-cli.js" '
        '"$npm_config_prefix/bin/playwright-cli"\n'
        'printf "registry=%s\\n" "$npm_config_registry" > "$npm_config_prefix/npm-args"\n'
        'printf "userconfig=%s\\n" "${npm_config_userconfig:-unset}" >> "$npm_config_prefix/npm-args"\n'
        'printf "globalconfig=%s\\n" "${npm_config_globalconfig:-unset}" >> "$npm_config_prefix/npm-args"\n'
        'printf "skip_browsers=%s\\n" "${PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD:-unset}" >> "$npm_config_prefix/npm-args"\n'
        'printf "download_host=%s\\n" "${PLAYWRIGHT_DOWNLOAD_HOST:-unset}" >> "$npm_config_prefix/npm-args"\n'
        'printf "args=%s\\n" "$*" >> "$npm_config_prefix/npm-args"\n'
        "exit 0\n",
    )


def _env(tmp_path: Path, stubs: Path, *, isolated: bool = False) -> dict[str, str]:
    """Environment with HOME and the data home inside ``tmp_path``.

    ``isolated`` builds a PATH holding ONLY ``stubs`` plus symlinks to
    ``BASE_UTILS``, which is how a scenario removes a utility (curl) from the
    host. Otherwise the stubs merely lead the real PATH, so the stub `node` and
    `npm` win while everything else stays available.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if isolated:
        for util in BASE_UTILS:
            real = shutil.which(util)
            if real and not (stubs / util).exists():
                (stubs / util).symlink_to(real)
        path = str(stubs)
    else:
        path = f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}"
    # The installer calls `mktemp -d` for its Node staging, which honors TMPDIR.
    # Without this the staging tree lands in the HOST /tmp, which the
    # no-test-side-effects rule forbids -- a test must leave nothing outside its
    # own tmp dir.
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir(exist_ok=True)
    return {
        "PATH": path,
        "HOME": str(home),
        "TMPDIR": str(tmpdir),
        "KIROCREW_HOME": str(tmp_path / "datahome"),
        # A stray ambient value would otherwise leak the developer's own
        # registry (or a CI runner's) into the tests' assertions.
        "KIROCREW_NPM_REGISTRY": "",
    }


# Copied from the ambient environment; everything else is dropped. An ALLOWLIST is
# the only form that holds: this suite has now leaked through the installer's
# `mktemp`, PowerShell's startup state, npm's cache, and a version manager's own
# data directory, and each time the fix was to blocklist the variable that had
# just escaped. A tool this suite has never heard of -- the next version manager,
# the next package manager -- cannot be blocklisted in advance, so nothing is
# inherited unless it is named here.
#
# PATH is the point of the exercise: the tools have to be findable. The rest is
# what a process needs to start and decode text at all.
_INHERITED_ENV = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
    # Windows: a process cannot start without these, and PowerShell resolves
    # built-in commands through PATHEXT.
    "SystemRoot",
    "SystemDrive",
    "COMSPEC",
    "PATHEXT",
    "WINDIR",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)


def _isolated_tool_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """An environment built from `_INHERITED_ENV` whose every write target is
    inside tmp_path. Nothing else from the caller's environment survives.

    HOME isolation is unconditional and has no opt-out. A tool that cannot run
    under an isolated HOME -- a version-manager shim resolving a toolchain it has
    to provision first -- must be SKIPPED by its caller, never handed the real
    HOME: provisioning is exactly the side effect this constructor exists to
    prevent, and it writes far outside tmp_path.

    Dropping the ambient environment is what stops that provisioning from finding
    its own data directory by another route: `MISE_DATA_DIR`, `ASDF_DATA_DIR`,
    `NVM_DIR`, `VOLTA_HOME`, `PNPM_HOME` and every future equivalent name a
    version manager may use are simply not present.
    """
    home = tmp_path / "toolhome"
    home.mkdir(exist_ok=True)
    env = {
        name: os.environ[name]  # FORBIDDEN_INHERIT: the one sanctioned read
        for name in _INHERITED_ENV
        if name in os.environ
    }
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CACHE_HOME": str(home / "cache"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_STATE_HOME": str(home / "state"),
            "TMPDIR": str(tmp_path / "tmp"),
            "npm_config_cache": str(home / "npm-cache"),
            "npm_config_logs_dir": str(home / "npm-logs"),
            "npm_config_update_notifier": "false",
            "POWERSHELL_TELEMETRY_OPTOUT": "1",
            "POWERSHELL_UPDATECHECK": "Off",
        }
    )
    (tmp_path / "tmp").mkdir(exist_ok=True)
    env.update(extra)
    return env


#: Magic bytes of a native executable on the platforms the POSIX installer runs on:
#: ELF, and the four Mach-O encodings (thin, both endiannesses, and a fat binary).
_NATIVE_EXECUTABLE_MAGIC = (
    b"\x7fELF",
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",
    b"\xca\xfe\xba\xbe",
)


def _is_native_executable(path: Path) -> bool:
    """True when *path* is a real machine-code binary, not a script or a symlink to one.

    A version-manager shim (mise, asdf, nvm's lazy shims) is either a shell script
    or a symlink whose target is the manager's own binary; both are told apart from
    the tool they impersonate by what the resolved file IS, which does not change
    between runs.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(4)
    except OSError:
        return False
    return head.startswith(_NATIVE_EXECUTABLE_MAGIC) and os.access(path, os.X_OK)


def _native_tool_on_path(name: str) -> Path | None:
    """The first *name* on PATH that resolves to a native binary of that name.

    Why not ``shutil.which``: on a developer machine the first hit is usually a
    version-manager shim, and a shim resolves its toolchain THROUGH HOME. Under the
    isolated HOME this suite runs tools in, it either fails or blocks trying to
    provision a toolchain that is not there, and whether it does so within any
    given bound depends on the manager's cache state at that moment -- so a skip
    decided by running it was a race, and a run that reached the timeout had to
    SIGKILL the shim's process tree. Judging the candidate by its resolved file
    instead gives the same verdict on every run of one host: a real binary runs
    with no HOME at all, and a host with only shims skips every time, with a reason
    that says so.

    Windows has no shim problem here and its executables carry PATHEXT, so the
    ordinary lookup is the right one there.
    """
    if os.name == "nt":
        found = shutil.which(name)
        return Path(found) if found else None
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        try:
            resolved = (Path(entry) / name).resolve(strict=True)
        except OSError:
            continue
        if resolved.name == name and _is_native_executable(resolved):
            return resolved
    return None


def _run(
    tmp_path: Path,
    stubs: Path,
    *args: str,
    isolated: bool = False,
    extra_env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the installer. Its working directory is *cwd*, or tmp_path when unstated.

    Never the caller's: pytest's cwd is the checkout, and anything the installer
    resolves or drops relative to where it was started would land there.
    """
    env = _env(tmp_path, stubs, isolated=isolated)
    if extra_env:
        env.update(extra_env)
    return run_bounded(["sh", str(INSTALLER_SH), *args], env=env, cwd=str(cwd or tmp_path))


@pytest.fixture(name="stubs")
def stubs_fixture(tmp_path: Path) -> Path:
    directory = tmp_path / "stubs"
    directory.mkdir()
    return directory


def _usable_powershell(tmp_path: Path) -> str:
    """A PowerShell binary this suite can drive under an isolated HOME, or a skip.

    Decided by what is on disk, never by running a candidate against a clock. A
    version manager installs `pwsh` as a SHIM that resolves its toolchain through
    config under HOME, so with HOME isolated the shim blocks trying to provision
    one -- and the earlier preflight, which ran it for ten seconds and skipped on
    the timeout, could answer either way depending on the manager's cache state,
    and every timeout ended in a SIGKILL of the shim's process tree. A native
    interpreter is recognised as such (``_native_tool_on_path``) and then run;
    a host that offers only shims skips with the same reason on every run.
    """
    shell = _native_tool_on_path("pwsh") or _native_tool_on_path("powershell")
    if shell is None:
        pytest.skip(
            "no native PowerShell binary on PATH (a version-manager shim does not "
            "count: it cannot start under the isolated HOME this suite requires)"
        )
    probe = run_bounded(
        [str(shell), "-NoProfile", "-NonInteractive", "-Command", "exit 0"],
        env=_isolated_tool_env(tmp_path),
        timeout=60,
        cwd=str(tmp_path),
    )
    if probe.returncode != 0:
        # A native interpreter that exits non-zero on `exit 0` is a host whose
        # PowerShell install is broken (missing ICU, unreadable modules): the same
        # answer on every run, so a skip that names it is stable. A hang is NOT
        # skipped: run_bounded's TimeoutExpired propagates as the error it is.
        pytest.skip(f"{shell} did not start cleanly: {probe.stdout + probe.stderr}")
    return str(shell)


def _expected_node_base(version: str) -> str:
    """The artifact basename the installer will ask for ON THIS HOST.

    Hardcoding one platform would pass on the Linux shard and fail on the macOS
    and arm64 runners. Linux/x64 also needs the native libc version so the
    fixture serves the older-glibc artifact when the installer requests it.
    """
    system = platform.system()
    node_os = {"Darwin": "darwin", "Linux": "linux"}[system]
    node_arch = {
        "x86_64": "x64",
        "amd64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
        "armv7l": "armv7l",
    }[platform.machine().lower()]
    if node_os == "linux" and node_arch == "x64":
        try:
            libc = os.confstr("CS_GNU_LIBC_VERSION") or ""
        except (OSError, ValueError):
            libc = ""
        match = re.fullmatch(r"glibc (\d+)\.(\d+)(?:\.\d+)*", libc)
        if match and (int(match[1]), int(match[2])) < (2, 28):
            return f"node-v{version}-linux-x64-glibc-217"
    return f"node-v{version}-{node_os}-{node_arch}"


def _node_tarball(tmp_path: Path, base: str) -> Path:
    """Build a tarball shaped like a real Node release: `<base>/bin/{node,npm}`."""
    tree = tmp_path / "nodesrc" / base / "bin"
    tree.mkdir(parents=True)
    _write_stub(
        tree / "node",
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"process.execPath"*) printf "%s\\n" "$0" ;;\n'
        '  *"process.versions.node.split"*) echo 22 ;;\n'
        '  *"process.versions.node"*) echo 22.0.0 ;;\n'
        '  *"playwright-cli.js"*) exec "$@" ;;\n'
        "  *) : ;;\n"
        "esac\n"
        "exit 0\n",
    )
    _write_stub(
        tree / "npm",
        "#!/bin/sh\n"
        'mkdir -p "$npm_config_prefix/bin" '
        '"$npm_config_prefix/lib/node_modules/@playwright/cli"\n'
        'printf "#!/bin/sh\\necho 0.1.18\\n" > '
        '"$npm_config_prefix/lib/node_modules/@playwright/cli/playwright-cli.js"\n'
        'chmod 755 "$npm_config_prefix/lib/node_modules/@playwright/cli/playwright-cli.js"\n'
        'ln -sf "../lib/node_modules/@playwright/cli/playwright-cli.js" '
        '"$npm_config_prefix/bin/playwright-cli"\n'
        "exit 0\n",
    )
    archive = tmp_path / f"{base}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(tree.parent, arcname=base)
    return archive


def _fake_curl(stubs: Path, served: Path) -> None:
    """A `curl` that serves `served/<basename-of-url>` instead of the network.

    Only the flag shape the installer actually uses is handled; anything else
    exits non-zero so a changed call site surfaces as a test failure rather than
    a silent pass.
    """
    _write_stub(
        stubs / "curl",
        "#!/bin/sh\n"
        'url=""; dest=""\n'
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in\n'
        '    -o) dest="$2"; shift 2 ;;\n'
        "    -*) shift ;;\n"
        '    *) url="$1"; shift ;;\n'
        "  esac\n"
        "done\n"
        '[ -n "$url" ] && [ -n "$dest" ] || exit 64\n'
        'name="${url##*/}"\n'
        f'[ -f "{served}/$name" ] || exit 22\n'
        f'cat "{served}/$name" > "$dest"\n',
    )


@pytest.fixture(name="node_mirror")
def node_mirror_fixture(tmp_path: Path) -> Path:
    directory = tmp_path / "mirror"
    directory.mkdir()
    return directory
