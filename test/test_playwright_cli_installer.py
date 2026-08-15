"""Coverage for the Playwright CLI installers (`playwright-cli.sh` / `.ps1`).

These installers are the one code path a user runs BEFORE they have a working
Node toolchain, so a failure here is unrecoverable from inside the product: the
user sees a shell error and has nothing to fall back on. CI has no shellcheck or
PowerShell lint job, and the interesting behavior — which Node build is chosen
for a platform, whether a tampered download is rejected, and which remedy an
enterprise-network failure prints — cannot be reviewed by reading either script.

Everything here is hermetic. `node`, `npm` and `curl` are stubs on a PATH the
test controls, `HOME` and `KIROCREW_HOME` point into `tmp_path`, and no test
reaches the network or writes outside its own temp dir.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import time
from pathlib import Path

import pytest
from installer_test_helpers import run_bounded

#: The Node version the bootstrap fixtures build tarballs for. Asserted equal to
#: both scripts' own default, so a bump cannot leave these tests exercising a
#: version the installer no longer ships.
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
        '  *"process.versions.node.split"*) echo "${PWCLI_FAKE_NODE_MAJOR:-22}" ;;\n'
        '  *"process.versions.node"*) echo "${PWCLI_FAKE_NODE_MAJOR:-22}.0.0" ;;\n'
        '  -e*) printf "%s" "${PWCLI_FAKE_INSTALLED_VERSION:-}" ;;\n'
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
            '    > "$(dirname "$0")/../install-browser-called"\n'
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
        'mkdir -p "$npm_config_prefix/bin"\n'
        # QUOTED delimiter, and the marker path is resolved from $0 at RUN time
        # rather than interpolated at write time. With an unquoted `<<EOF` the
        # shell expanded `$npm_config_prefix` into the generated script as live
        # source, so a prefix containing `$(...)` -- which
        # `test_wrapper_generation_escapes_the_paths_it_embeds` deliberately
        # supplies -- executed when the installer ran this stub. That made the
        # one test guarding the installer's path escaping inject from its own
        # fixture instead, and it wrote its marker into the process CWD where
        # that test's `tmp_path` assertion could not see it.
        "cat > \"$npm_config_prefix/bin/playwright-cli\" <<'EOF'\n"
        "#!/bin/sh\n"
        f'[ "$1" = "--version" ] && echo "{version}" && exit 0\n'
        'if [ "$1" = "install-browser" ]; then\n'
        f"{browser_step}"
        "fi\n"
        "exit 0\n"
        "EOF\n"
        'chmod 755 "$npm_config_prefix/bin/playwright-cli"\n'
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
        # registry (or a CI runner's) into the assertions below.
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


def _run(
    tmp_path: Path,
    stubs: Path,
    *args: str,
    isolated: bool = False,
    extra_env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = _env(tmp_path, stubs, isolated=isolated)
    if extra_env:
        env.update(extra_env)
    return run_bounded(["sh", str(INSTALLER_SH), *args], env=env, cwd=str(cwd) if cwd else None)


@pytest.fixture()
def stubs(tmp_path: Path) -> Path:
    directory = tmp_path / "stubs"
    directory.mkdir()
    return directory


# ── syntax ───────────────────────────────────────────────────────────


@posix_only
def test_installer_parses_as_posix_shell() -> None:
    """The documented entry point is `curl … | sh`, so the script must parse
    under a plain POSIX shell and not only under bash."""
    subprocess.run(["sh", "-n", str(INSTALLER_SH)], check=True)


@posix_only
@pytest.mark.skipif(shutil.which("ksh") is None, reason="no non-bash POSIX shell available here")
def test_installer_parses_under_a_non_bash_shell() -> None:
    """On most Linux distributions /bin/sh IS bash, so `sh -n` above happily
    accepts bashisms that dash and BusyBox ash reject. ksh is a genuinely
    different POSIX implementation, so parsing there catches what `sh -n` cannot
    on a bash-provided /bin/sh."""
    subprocess.run(["ksh", "-n", str(INSTALLER_SH)], check=True)


def test_installer_avoids_bash_only_syntax() -> None:
    """`sh -n` on a bash-provided /bin/sh accepts bashisms that dash rejects, so
    the constructs that actually break on Alpine/Debian are checked directly."""
    # Whole-line comments are stripped first: the prose explaining WHY a
    # construct is avoided naturally names it, and matching that would make the
    # check fire on its own rationale.
    body = "\n".join(
        line for line in INSTALLER_SH.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    for pattern, description in (
        (r"\[\[", "[[ ... ]] is a bash conditional"),
        (r"^\s*local\s", "`local` is not POSIX"),
        (r"\bfunction\s+\w+\s*\(", "`function name()` is bash-only"),
        (r"\$\{[A-Za-z_][A-Za-z0-9_]*\[", "array subscripting is bash-only"),
        (r"&>", "&> redirection is bash-only"),
        (r"\becho\s+-e\b", "echo -e is not portable"),
        (r"\bsort\s+-V\b", "sort -V is not POSIX and is absent from BusyBox"),
        (r"\b(head|tail)\s+-[0-9]", "head -N / tail -N are obsolescent; POSIX is -n N"),
    ):
        assert not re.search(pattern, body, re.MULTILINE), description


def _usable_powershell(tmp_path: Path) -> str:
    """A PowerShell that actually answers under an isolated HOME, or a skip.

    Same hazard as the real-npm probe, and it bit for the same reason: a version
    manager installs `pwsh` as a SHIM that resolves its toolchain through config
    under HOME, so with HOME isolated the shim blocks trying to provision one.
    Without this preflight each PowerShell test burned its full 120s timeout on a
    developer machine with mise-managed PowerShell -- four minutes to say nothing.
    A real interpreter answers `exit 0` instantly, which is the whole probe.
    """
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("no PowerShell on this host")
    try:
        probe = run_bounded(
            [shell, "-NoProfile", "-NonInteractive", "-Command", "exit 0"],
            env=_isolated_tool_env(tmp_path),
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        pytest.skip(f"{shell} cannot run under an isolated HOME (version-manager shim)")
    if probe.returncode != 0:
        pytest.skip(f"{shell} did not start cleanly: {probe.stdout + probe.stderr}")
    return shell


@pytest.mark.skipif(
    shutil.which("pwsh") is None and shutil.which("powershell") is None,
    reason="no PowerShell available to parse playwright-cli.ps1",
)
def test_powershell_installer_parses(tmp_path: Path) -> None:
    """A syntax error in the Windows installer reaches every Windows user, and
    no CI job lints PowerShell — so parse it wherever a shell exists.

    PowerShell writes module/telemetry state under HOME on startup, so the
    interpreter runs with HOME and the XDG dirs pinned inside tmp_path: a test
    must leave nothing outside its own directory.
    """
    shell = _usable_powershell(tmp_path)
    script = (
        "$errors = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{INSTALLER_PS1}', [ref]$null, [ref]$errors); "
        "if ($errors) { $errors | ForEach-Object { $_.Message }; exit 1 }"
    )
    env = _isolated_tool_env(tmp_path)
    result = run_bounded(
        [shell, "-NoProfile", "-NonInteractive", "-Command", script],
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ── contract: help, flags, exit codes ────────────────────────────────


@posix_only
def test_help_documents_every_accepted_flag(tmp_path: Path, stubs: Path) -> None:
    """`--help` is the only documentation a piped-in installer can offer, so an
    added flag that never reaches the help text is a silent feature."""
    body = INSTALLER_SH.read_text()
    parser = body.split("while [ $# -gt 0 ]; do", 1)[1].split("done", 1)[0]
    flags = {
        match.group(1) for match in re.finditer(r"^\s*(--[a-z][a-z-]*)\)", parser, re.MULTILINE)
    }
    assert flags, "no flags found — the arg-parsing loop moved"

    result = _run(tmp_path, stubs, "--help")
    assert result.returncode == 0, result.stderr
    for flag in flags:
        assert flag in result.stdout, f"{flag} is accepted but undocumented"


@posix_only
def test_unknown_argument_is_a_usage_error(tmp_path: Path, stubs: Path) -> None:
    result = _run(tmp_path, stubs, "--not-a-flag")
    assert result.returncode == EXIT_CODES["usage"]
    assert "unknown argument" in result.stderr


@posix_only
@pytest.mark.parametrize(
    "flag",
    ["--registry", "--node-mirror", "--download-host"],
)
def test_plain_http_urls_are_refused(tmp_path: Path, stubs: Path, flag: str) -> None:
    """Every one of these URLs delivers bytes this installer then executes, and
    the Node checksum manifest travels the same channel it protects — so a
    downgrade to http would make verification meaningless."""
    result = _run(tmp_path, stubs, flag, "http://mirror.internal.example/")
    assert result.returncode == EXIT_CODES["usage"]
    assert "must be an https:// URL" in result.stderr


@posix_only
def test_an_ambient_plain_http_browser_mirror_is_refused(tmp_path: Path, stubs: Path) -> None:
    """PLAYWRIGHT_DOWNLOAD_HOST reaches `npx playwright install` by inheritance,
    not because this script exports it — so validating only the flag would let
    an http:// mirror already in the environment deliver browser executables
    over plaintext, which this installer then launches."""
    result = _run(
        tmp_path,
        stubs,
        extra_env={"PLAYWRIGHT_DOWNLOAD_HOST": "http://mirror.internal.example/"},
    )
    assert result.returncode == EXIT_CODES["usage"]
    assert "must be an https:// URL" in result.stderr


def test_powershell_seeds_the_browser_mirror_from_the_environment() -> None:
    """Parity for the check above: the ps1 parameter defaults to empty, so the
    ambient value has to be read into it before Require-Https runs."""
    body = INSTALLER_PS1.read_text(encoding="utf-8")
    assert "$env:PLAYWRIGHT_DOWNLOAD_HOST" in body
    seed = body.index("$env:PLAYWRIGHT_DOWNLOAD_HOST")
    check = body.index('Require-Https "-DownloadHost"')
    assert seed < check, "the ambient mirror must be seeded before it is validated"


@posix_only
def test_dry_run_touches_nothing(tmp_path: Path, stubs: Path) -> None:
    """A user auditing an installer before running it needs a mode that reports
    the plan without side effects."""
    _fake_node(stubs)
    result = _run(tmp_path, stubs, "--dry-run", "--version", "0.1.18")
    assert result.returncode == 0, result.stderr
    assert "@playwright/cli@0.1.18" in result.stdout
    assert "reuse" in result.stdout
    assert not (tmp_path / "datahome").exists()
    assert not (tmp_path / "home" / ".local").exists()


@posix_only
def test_missing_downloader_is_reported_before_anything_else(tmp_path: Path, stubs: Path) -> None:
    result = _run(tmp_path, stubs, "--dry-run", isolated=True)
    assert result.returncode == EXIT_CODES["missing_tool"]
    assert "curl or wget" in result.stderr


# ── enterprise-network failure classification ────────────────────────

# Real npm output for the failures a corporate network produces. The whole point
# of the classifier is that a user who hits one of these is told which command
# fixes THEIR case, so each fixture asserts the remedy, not just the exit code.
NPM_FAILURES = [
    pytest.param(
        "npm error code E401\n"
        "npm error Incorrect or missing password.\n"
        "npm error If you were trying to login, change your password, create an\n"
        "npm error authentication token or enable two-factor authentication then\n"
        "npm error that means you likely typed your password in incorrectly.\n",
        "registry_auth",
        "--isolated-npmrc",
        id="e401-expired-mirror-token",
    ),
    pytest.param(
        "npm error code E403\n"
        "npm error 403 403 Forbidden - GET https://npm.internal.example/@playwright%2fcli\n",
        "registry_auth",
        "npm login --registry",
        id="e403-forbidden",
    ),
    pytest.param(
        "npm error code ENEEDAUTH\n"
        "npm error need auth This command requires you to be logged in.\n",
        "registry_auth",
        "--isolated-npmrc",
        id="eneedauth",
    ),
    pytest.param(
        "npm error code ENOTFOUND\n"
        "npm error syscall getaddrinfo\n"
        "npm error request to https://registry.npmjs.org/@playwright%2fcli failed,"
        " reason: getaddrinfo ENOTFOUND registry.npmjs.org\n",
        "registry_unreachable",
        "HTTPS_PROXY",
        id="dns-blocked",
    ),
    pytest.param(
        "npm error code SELF_SIGNED_CERT_IN_CHAIN\n"
        "npm error request to https://registry.npmjs.org/ failed, reason:"
        " self signed certificate in certificate chain\n",
        "registry_unreachable",
        "NODE_EXTRA_CA_CERTS",
        id="tls-terminating-proxy",
    ),
    pytest.param(
        "npm error code ECONNREFUSED\n"
        "npm error tunneling socket could not be established, cause=connect ECONNREFUSED\n",
        "registry_unreachable",
        "HTTPS_PROXY",
        id="proxy-refused",
    ),
    pytest.param(
        "npm error code ETARGET\n"
        "npm error notarget No matching version found for @playwright/cli@0.0.1.\n",
        "package_not_found",
        "npm view @playwright/cli versions",
        id="version-absent-from-mirror",
    ),
    pytest.param(
        "npm error code E404\n"
        "npm error 404 Not Found - GET https://npm.internal.example/@playwright%2fcli\n",
        "package_not_found",
        "npm view @playwright/cli versions",
        id="package-never-proxied",
    ),
    pytest.param(
        "Downloading Chromium 140.0 from https://cdn.playwright.dev/dbazure/download"
        "/playwright/builds/chromium/chromium-linux.zip\n"
        "Failed to download chromium, caused by\n"
        "Error: connect ETIMEDOUT 13.107.246.45:443\n",
        "browser_download",
        "--download-host",
        id="browser-cdn-blocked",
    ),
    pytest.param(
        "npm error code EMYSTERY\n" "npm error something nobody has seen before\n",
        None,
        "matched no known cause",
        id="unclassified",
    ),
]


@posix_only
@pytest.mark.parametrize("npm_output,expected_key,expected_remedy", NPM_FAILURES)
def test_npm_failures_are_classified_with_a_remedy(
    tmp_path: Path,
    stubs: Path,
    npm_output: str,
    expected_key: str | None,
    expected_remedy: str,
) -> None:
    _fake_node(stubs)
    _fake_npm_failing(stubs, npm_output)
    result = _run(tmp_path, stubs, "--version", "0.1.18")

    expected_code = 1 if expected_key is None else EXIT_CODES[expected_key]
    assert result.returncode == expected_code, result.stderr
    assert expected_remedy in result.stderr
    # The classifier reads npm's output from a file, so that file must survive
    # for the user to read after the diagnostic points at it.
    log = tmp_path / "datahome" / "playwright-cli" / "playwright-cli-install.log"
    assert log.exists()
    assert "npm error" in log.read_text() or "Failed to download" in log.read_text()
    assert str(log) in result.stderr


@posix_only
def test_browser_cdn_failure_outranks_the_generic_transport_error(
    tmp_path: Path, stubs: Path
) -> None:
    """A blocked browser CDN reports the SAME ETIMEDOUT/ECONNREFUSED as a blocked
    registry, so the ordering of the classifier decides whether the user is told
    to fix their proxy (wrong) or to mirror the browsers (right)."""
    _fake_node(stubs)
    _fake_npm_failing(
        stubs,
        "npm error code ECONNREFUSED\n"
        "Failed to download chromium from https://cdn.playwright.dev/builds/chromium.zip\n"
        "Error: connect ECONNREFUSED 13.107.246.45:443\n",
    )
    result = _run(tmp_path, stubs, "--version", "0.1.18")
    assert result.returncode == EXIT_CODES["browser_download"]


# ── success path ─────────────────────────────────────────────────────


@posix_only
def test_successful_install_writes_a_node_pinning_wrapper(tmp_path: Path, stubs: Path) -> None:
    """The wrapper — not a symlink — is what makes a bootstrapped Node usable: an
    npm-generated `#!/usr/bin/env node` shim resolves against the CALLER's PATH,
    so a user with no Node on PATH would get "node: not found" from a tool that
    installed cleanly."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(tmp_path, stubs, "--version", "0.1.18")
    assert result.returncode == 0, result.stderr

    wrapper = tmp_path / "home" / ".local" / "bin" / "playwright-cli"
    assert wrapper.exists()
    assert os.access(wrapper, os.X_OK)
    body = wrapper.read_text()
    assert str(stubs) in body, "the wrapper must pin the Node the installer verified"
    assert "exec " in body
    # No half-written wrapper is left behind by the atomic move.
    assert not (wrapper.parent / "playwright-cli.incoming").exists()

    # The installer reports the version it got from the tool it just installed,
    # which is what proves the wrapper actually runs.
    assert "0.1.18" in result.stdout


@posix_only
def test_install_pins_the_public_registry_and_honors_flags(tmp_path: Path, stubs: Path) -> None:
    """An ambient .npmrc pointing at an expired private mirror is the most common
    enterprise failure, so the public registry is pinned by default rather than
    inherited; --isolated-npmrc goes further and ignores the config entirely."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        "--isolated-npmrc",
        "--skip-browsers",
        "--download-host",
        "https://playwright.internal.example/",
    )
    assert result.returncode == 0, result.stderr
    recorded = (tmp_path / "datahome" / "playwright-cli" / "npm-args").read_text()
    assert "registry=https://registry.npmjs.org/" in recorded
    # Two DISTINCT empty scope files, both inside the prefix. npm rejects one path
    # used as two scopes, so asserting a single sentinel here (as this once did)
    # pinned a flag that npm refuses outright.
    recorded_lines = recorded.splitlines()
    user_line = next(x for x in recorded_lines if x.startswith("userconfig="))
    global_line = next(x for x in recorded_lines if x.startswith("globalconfig="))
    assert user_line != global_line
    assert user_line.endswith("/isolated-npmrc/user")
    assert global_line.endswith("/isolated-npmrc/global")
    assert "skip_browsers=1" in recorded
    assert "download_host=https://playwright.internal.example/" in recorded
    assert "args=install --global @playwright/cli@0.1.18" in recorded


@posix_only
def test_a_corporate_registry_overrides_the_default(tmp_path: Path, stubs: Path) -> None:
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        "--registry",
        "https://npm.internal.example/",
    )
    assert result.returncode == 0, result.stderr
    recorded = (tmp_path / "datahome" / "playwright-cli" / "npm-args").read_text()
    assert "registry=https://npm.internal.example/" in recorded


@posix_only
def test_a_pinned_version_already_present_is_not_reinstalled(tmp_path: Path, stubs: Path) -> None:
    """Re-running the one-liner is the normal way users upgrade or repair, so the
    common case must not re-download an unchanged pinned version."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    first = _run(tmp_path, stubs, "--version", "0.1.18")
    assert first.returncode == 0, first.stderr

    marker = tmp_path / "datahome" / "playwright-cli" / "npm-args"
    marker.unlink()
    second = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        extra_env={"PWCLI_FAKE_INSTALLED_VERSION": "0.1.18"},
    )
    assert second.returncode == 0, second.stderr
    assert "already installed" in second.stdout
    assert not marker.exists(), "npm ran again despite the pinned version being present"

    forced = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        "--force",
        extra_env={"PWCLI_FAKE_INSTALLED_VERSION": "0.1.18"},
    )
    assert forced.returncode == 0, forced.stderr
    assert marker.exists(), "--force must reinstall"


@posix_only
def test_latest_is_never_treated_as_already_installed(tmp_path: Path, stubs: Path) -> None:
    """`latest` is a moving target: skipping the install because some version is
    present would silently pin the user to whatever they first installed."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(tmp_path, stubs, extra_env={"PWCLI_FAKE_INSTALLED_VERSION": "0.1.18"})
    assert result.returncode == 0, result.stderr
    assert "already installed" not in result.stdout
    assert (tmp_path / "datahome" / "playwright-cli" / "npm-args").exists()


@posix_only
def test_too_old_node_on_path_is_rejected_not_used(tmp_path: Path, stubs: Path) -> None:
    """An existing Node below the package's floor must trigger a bootstrap rather
    than being used and failing later with an opaque engine error."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(
        tmp_path,
        stubs,
        "--dry-run",
        extra_env={"PWCLI_FAKE_NODE_MAJOR": "16"},
    )
    assert result.returncode == 0, result.stderr
    assert "node           install" in result.stdout


@posix_only
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory permissions, so the refusal cannot be provoked",
)
def test_unwritable_prefix_is_reported_as_such(tmp_path: Path, stubs: Path) -> None:
    _fake_node(stubs)
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        result = _run(tmp_path, stubs, "--prefix", str(blocked / "prefix"))
        assert result.returncode == EXIT_CODES["not_writable"]
        assert "prefix" in result.stderr
    finally:
        blocked.chmod(0o700)


# ── Node bootstrap ───────────────────────────────────────────────────


def _expected_node_base(version: str) -> str:
    """The artifact basename the installer will ask for ON THIS HOST.

    Hardcoding one platform would pass on the Linux shard and fail on the macOS
    and arm64 runners, so the mapping mirrors the script's own uname cases.
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
    return f"node-v{version}-{node_os}-{node_arch}"


def _node_tarball(tmp_path: Path, base: str) -> Path:
    """Build a tarball shaped like a real Node release: `<base>/bin/{node,npm}`."""
    tree = tmp_path / "nodesrc" / base / "bin"
    tree.mkdir(parents=True)
    _write_stub(
        tree / "node",
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"process.versions.node.split"*) echo 22 ;;\n'
        '  *"process.versions.node"*) echo 22.0.0 ;;\n'
        "  *) : ;;\n"
        "esac\n"
        "exit 0\n",
    )
    _write_stub(
        tree / "npm",
        "#!/bin/sh\n"
        'mkdir -p "$npm_config_prefix/bin"\n'
        'printf "#!/bin/sh\\necho 0.1.18\\n" > "$npm_config_prefix/bin/playwright-cli"\n'
        'chmod 755 "$npm_config_prefix/bin/playwright-cli"\n'
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


@pytest.fixture()
def node_mirror(tmp_path: Path) -> Path:
    directory = tmp_path / "mirror"
    directory.mkdir()
    return directory


@posix_only
def test_node_bootstrap_verifies_the_download_and_then_installs(
    tmp_path: Path, stubs: Path, node_mirror: Path
) -> None:
    """A bootstrapped Node is downloaded from the internet and then EXECUTED, so
    it is only ever trusted after its release manifest checksum matches."""
    base = _expected_node_base(TESTED_NODE_VERSION)
    archive = _node_tarball(tmp_path, base)
    shutil.copy(archive, node_mirror / archive.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (node_mirror / "SHASUMS256.txt").write_text(
        f"{'0' * 64}  node-v{TESTED_NODE_VERSION}-aix-ppc64.tar.gz\n" f"{digest}  {archive.name}\n"
    )
    _fake_curl(stubs, node_mirror)

    result = _run(
        tmp_path,
        stubs,
        "--node-mirror",
        "https://node.internal.example/dist",
        "--node-version",
        TESTED_NODE_VERSION,
        isolated=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "verified Node SHA-256" in result.stdout
    installed = tmp_path / "datahome" / "playwright-cli" / "node" / "bin" / "node"
    assert installed.exists()
    # An interrupted extract must not leave a tree the next run mistakes for a
    # complete install.
    assert not (tmp_path / "datahome" / "playwright-cli" / "node.incoming").exists()


@posix_only
def test_node_bootstrap_refuses_a_tampered_archive(
    tmp_path: Path, stubs: Path, node_mirror: Path
) -> None:
    base = _expected_node_base(TESTED_NODE_VERSION)
    archive = _node_tarball(tmp_path, base)
    shutil.copy(archive, node_mirror / archive.name)
    (node_mirror / "SHASUMS256.txt").write_text(f"{'0' * 64}  {archive.name}\n")
    _fake_curl(stubs, node_mirror)

    result = _run(
        tmp_path,
        stubs,
        "--node-mirror",
        "https://node.internal.example/dist",
        "--node-version",
        TESTED_NODE_VERSION,
        isolated=True,
    )
    assert result.returncode == EXIT_CODES["checksum"]
    assert "checksum mismatch" in result.stderr
    assert not (tmp_path / "datahome" / "playwright-cli" / "node").exists()


@posix_only
def test_node_bootstrap_refuses_an_archive_absent_from_the_manifest(
    tmp_path: Path, stubs: Path, node_mirror: Path
) -> None:
    """A manifest that simply omits our artifact must fail closed: treating a
    missing line as "nothing to check" would skip verification entirely."""
    base = _expected_node_base(TESTED_NODE_VERSION)
    archive = _node_tarball(tmp_path, base)
    shutil.copy(archive, node_mirror / archive.name)
    other = f"node-v{TESTED_NODE_VERSION}-aix-ppc64.tar.gz"
    assert other != archive.name
    (node_mirror / "SHASUMS256.txt").write_text(f"{'0' * 64}  {other}\n")
    _fake_curl(stubs, node_mirror)

    result = _run(
        tmp_path,
        stubs,
        "--node-mirror",
        "https://node.internal.example/dist",
        "--node-version",
        TESTED_NODE_VERSION,
        isolated=True,
    )
    assert result.returncode == EXIT_CODES["checksum"]
    assert "absent from" in result.stderr


@posix_only
def test_unreachable_node_mirror_is_reported_with_the_mirror_flag(
    tmp_path: Path, stubs: Path, node_mirror: Path
) -> None:
    _fake_curl(stubs, node_mirror)  # serves nothing
    result = _run(
        tmp_path,
        stubs,
        "--node-mirror",
        "https://node.internal.example/dist",
        isolated=True,
    )
    assert result.returncode == EXIT_CODES["node_bootstrap"]
    assert "--node-mirror" in result.stderr


def test_musl_and_old_glibc_hosts_select_an_executable_node_build() -> None:
    """nodejs.org publishes no musl build and nothing for pre-2.28 glibc: an
    official tarball on Alpine reports "not found", and on a RHEL 7-era host
    fails a GLIBC_2.28 symbol lookup. Both need the unofficial-builds mirror."""
    body = INSTALLER_SH.read_text()
    selector = body.split("_resolve_node_artifact() {", 1)[1].split("\n}", 1)[0]
    assert "musl" in selector
    assert "glibc-217" in selector
    assert "NODE_UNOFFICIAL_MIRROR" in selector
    # The glibc-217 variant is published for x64 only, so selecting it on arm64
    # would 404 on a URL that never existed.
    assert 'NODE_ARCH" = "x64"' in selector


def test_an_unpublished_platform_pair_is_named_not_reported_as_a_network_fault() -> None:
    """No musl build exists for armv7l at any Node version. Letting that 404 come
    back as "check proxy access" sends the user to debug a firewall that is
    working, so the combination is refused up front -- and it must be refused on
    BOTH paths that can reach a download, including --dry-run's report."""
    body = INSTALLER_SH.read_text()
    guard = body.split("_assert_bootstrap_supported() {", 1)[1].split("\n}", 1)[0]
    assert "armv7l" in guard and "musl" in guard
    assert "EX_MISSING_TOOL" in guard
    assert body.count("_assert_bootstrap_supported\n") >= 2


# ── the wrapper is generated code, so its inputs must be escaped ─────


@posix_only
@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param("pre fix", id="space"),
        pytest.param("pre$(touch INJECTED)fix", id="command-substitution"),
        pytest.param("pre`touch INJECTED`fix", id="backtick"),
        pytest.param("pre'quote'fix", id="single-quote"),
        pytest.param("pre$HOMEfix", id="variable-expansion"),
    ],
)
def test_wrapper_generation_escapes_the_paths_it_embeds(
    tmp_path: Path, stubs: Path, hostile: str
) -> None:
    """The wrapper is a shell script this installer WRITES, so every path
    interpolated into it is code. Unescaped, a directory containing a space
    merely produces a broken wrapper, while one containing $(...) or a backtick
    executes when the wrapper is invoked."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    prefix = tmp_path / hostile / "prefix"
    bindir = tmp_path / "bin"
    # Both child processes run with their CWD inside tmp_path. A payload that
    # escapes would `touch INJECTED` relative to the CWD, so anchoring it here is
    # what puts the artifact where the assertion below looks. Left at the default
    # the marker landed in the repo root instead, and this test passed while its
    # own payload had executed.
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        "--prefix",
        str(prefix),
        "--bin-dir",
        str(bindir),
        cwd=workdir,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    wrapper = bindir / "playwright-cli"
    assert wrapper.exists()
    # Nothing embedded in the wrapper may have executed during generation or
    # during the installer's own verification run.
    assert not list(tmp_path.rglob("INJECTED"))
    # And the wrapper must actually work, which is what proves the escaping
    # preserved the path rather than merely neutering it.
    # Bounded and given an isolated environment: this spawns the wrapper, which
    # spawns node, so an unbounded call leaves a process tree behind on hang.
    ran = run_bounded(
        [str(wrapper), "--version"],
        env=_isolated_tool_env(tmp_path),
        cwd=str(workdir),
        timeout=120,
    )
    assert ran.returncode == 0, ran.stderr
    assert "0.1.18" in ran.stdout
    # Re-checked after the wrapper ran: the exec line is the other place an
    # unescaped path would fire.
    assert not list(tmp_path.rglob("INJECTED"))


# ── enterprise credential hygiene ────────────────────────────────────


@posix_only
def test_a_registry_url_carrying_credentials_is_never_printed(tmp_path: Path, stubs: Path) -> None:
    """The documented enterprise remedy is `--registry https://mirror/`, which
    invites a URL with an embedded token. The failure path echoes the registry
    AND dumps npm's log to stderr, so an unredacted value would land in a
    terminal, a CI log, or a pasted bug report."""
    _fake_node(stubs)
    _fake_npm_failing(
        stubs,
        "npm error code E401\nnpm error Incorrect or missing password.\n",
    )
    secret = "s3cr3t-token-value"
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        extra_env={"KIROCREW_NPM_REGISTRY": f"https://bob:{secret}@npm.internal.example/"},
    )
    assert result.returncode == EXIT_CODES["registry_auth"]
    combined = result.stdout + result.stderr
    assert secret not in combined
    assert "bob" not in combined
    assert "//***@npm.internal.example/" in combined
    # The host itself is still reported -- redaction must not cost the user the
    # one fact they need to fix their configuration.
    assert "npm.internal.example" in combined


@posix_only
def test_the_install_log_is_owner_only(tmp_path: Path, stubs: Path) -> None:
    """npm writes the registry URL into its output, so the log can hold a token.
    Under the common 022 umask a plain redirect would publish it to every
    account on the host."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(tmp_path, stubs, "--version", "0.1.18")
    assert result.returncode == 0, result.stderr
    log = tmp_path / "datahome" / "playwright-cli" / "playwright-cli-install.log"
    assert log.exists()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_both_installers_classify_the_same_browser_download_failures() -> None:
    """A custom browser mirror reports `Download failure` without naming the CDN,
    so a matcher that only knows the CDN hostnames misfiles it as a registry
    problem (exit 14) and prints the registry remedy -- to a user whose registry
    is fine. The two installers classify from the same npm output, so the set of
    alternatives has to agree; it drifted once already (the shell installer had
    this one, PowerShell did not).
    """
    alternative = r"Download failure.*(chromium|firefox|webkit)"
    for installer in (INSTALLER_SH, INSTALLER_PS1):
        assert alternative in installer.read_text(), (
            f"{installer.name} cannot classify a mirror's 'Download failure' as a "
            "browser problem, so it will print the registry remedy instead"
        )


def test_a_log_that_cannot_be_scrubbed_is_never_printed() -> None:
    """Both installers refuse to print an unscrubbed log, and neither may infer
    that from the file being absent: the shell empties it and PowerShell's delete
    is best-effort, so a log locked by a scanner survives with its token intact.
    The decision is carried in an explicit flag in both.
    """
    assert "LOG_SUPPRESSED=1" in INSTALLER_SH.read_text()
    ps1 = INSTALLER_PS1.read_text()
    assert "$script:LogSuppressed = $true" in ps1
    tail = ps1[ps1.index("function Show-LogTail") :]
    guard = tail.index("$script:LogSuppressed")
    printer = tail.index("-Tail 20")
    assert guard < printer, "Show-LogTail must consult the flag before printing the log"


@posix_only
def test_a_credential_containing_an_at_sign_is_fully_redacted(tmp_path: Path, stubs: Path) -> None:
    """A token may itself contain `@`. Matching only up to the FIRST one cuts the
    credential in half and leaves the remainder in the log -- `//***@ss@host/` for
    a password of `p@ss`. The match therefore runs to the last `@` in the
    authority, and excludes `/` so it cannot reach into the path.
    """
    secret = "p@ssw0rd-tail"
    registry = f"https://user:{secret}@npm.internal.example/"
    _fake_node(stubs)
    _fake_npm_failing(stubs, f"npm error code E401\nnpm error 401 for {registry}")
    # Through the environment, which is where a credentialed registry now has to
    # come from: as a flag it would be world-readable in /proc and is refused.
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        extra_env={"KIROCREW_NPM_REGISTRY": registry},
    )
    assert result.returncode == EXIT_CODES["registry_auth"]
    combined = result.stdout + result.stderr
    log = (tmp_path / "datahome" / "playwright-cli" / "playwright-cli-install.log").read_text()
    for haystack in (combined, log):
        assert secret not in haystack, haystack
        # The tail of the password must not survive either.
        assert "ssw0rd-tail" not in haystack, haystack
    assert "//***@npm.internal.example" in log


@posix_only
def test_redaction_does_not_touch_an_at_sign_in_a_path() -> None:
    """The greedy match must stay inside the authority: npm prints scoped package
    paths, and rewriting one would corrupt the log it exists to make readable.
    """
    script = INSTALLER_SH.read_text()
    body = script[script.index("_redact_urls() {") :]
    assert "[^/[:space:]]*@" in body
    probe = subprocess.run(
        ["sh", "-c", 'sed "s|//[^/[:space:]]*@|//***@|g"'],
        input="https://npm.example/@scope/pkg\nhttps://u:p@ss@npm.example/@scope/pkg\n",
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.splitlines()[0] == "https://npm.example/@scope/pkg"
    assert probe.stdout.splitlines()[1] == "https://***@npm.example/@scope/pkg"


@posix_only
def test_a_relative_prefix_is_absolute_in_the_generated_wrapper(
    tmp_path: Path, stubs: Path
) -> None:
    """The wrapper outlives the install, so a relative --prefix would resolve
    against whatever directory the CALLER is in -- it works from the install
    directory and nowhere else. Every path baked into the wrapper is absolute.
    """
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    workdir = tmp_path / "work"
    workdir.mkdir()
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        "--prefix",
        "./relative-prefix",
        "--bin-dir",
        "./relative-bin",
        cwd=workdir,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wrapper = workdir / "relative-bin" / "playwright-cli"
    assert wrapper.is_file(), sorted(p.name for p in workdir.iterdir())
    body = wrapper.read_text()
    for line in body.splitlines():
        assert "./relative" not in line, body
    assert str(workdir) in body, body


@pytest.mark.skipif(
    shutil.which("pwsh") is None and shutil.which("powershell") is None,
    reason="no PowerShell available to execute the installer's own helpers",
)
def test_the_powershell_helpers_behave_when_actually_executed(tmp_path: Path) -> None:
    """Everything else about the .ps1 in this suite is asserted from its TEXT,
    which is how four defects reached review -- text cannot tell you that a
    regex redacts what you meant or that a path API normalises. These two helpers
    are extracted from the shipped file and RUN, so the assertions are about
    behaviour. The script itself cannot be dot-sourced: it exits early off
    Windows, by design.
    """
    body = INSTALLER_PS1.read_text()

    def _function(name: str) -> str:
        """Whole block, not one line: these helpers grow multi-line as the shapes
        they handle accumulate, and a line-based extractor silently truncates one
        into a syntax error."""
        start = body.index(f"function {name}")
        return body[start : body.index("\n}", start) + 2]

    script = (
        f"{_function('Redact-Url')}\n{_function('Get-AbsolutePath')}\n"
        f"Set-Location -LiteralPath '{tmp_path}'\n"
        "Write-Output (Redact-Url 'https://user:p@ss@npm.example/x')\n"
        "Write-Output (Redact-Url 'https://npm.example/@scope/pkg')\n"
        "Write-Output (Redact-Url 'https://mirror.example/?token=SECRET')\n"
        "Write-Output (Get-AbsolutePath './relative-prefix')\n"
    )
    shell = _usable_powershell(tmp_path)
    result = run_bounded(
        [shell, "-NoProfile", "-NonInteractive", "-Command", script],
        env=_isolated_tool_env(tmp_path),
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    credential, path_at, query, resolved = result.stdout.split()[:4]
    # A token containing '@' is redacted whole, not cut at the first one.
    assert credential == "https://***@npm.example/x"
    # An '@' in a PATH is left alone: npm prints scoped package names.
    assert path_at == "https://npm.example/@scope/pkg"
    # A mirror that authenticates by query parameter is redacted too.
    assert query == "https://mirror.example/?***"
    # Absolute AND normalised, which is why the .ps1 needs no second
    # canonicalisation step where the shell installer does.
    assert resolved == str(tmp_path / "relative-prefix")


@posix_only
@pytest.mark.parametrize("root", ["/", "//", "///"])
def test_a_filesystem_root_prefix_is_refused_not_silently_relocated(
    tmp_path: Path, stubs: Path, root: str
) -> None:
    """Stripping trailing separators reduces a root to the empty string, which
    absolutising then resolves against $PWD -- so `--prefix /` would install into
    whatever directory the user happened to be in. Refused with a usage error
    instead: a filesystem root is not a private per-user prefix, and preserving
    "/" would build "//node", which POSIX leaves implementation-defined.
    """
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    result = _run(tmp_path, stubs, "--version", "0.1.18", "--prefix", root, cwd=workdir)
    assert result.returncode == EXIT_CODES["usage"], result.stdout + result.stderr
    assert "may not be the filesystem root" in result.stderr
    # Nothing may have been written into the working directory.
    assert sorted(p.name for p in workdir.iterdir()) == []


def test_the_windows_script_refuses_root_and_drive_relative_prefixes() -> None:
    """`C:\\` trims to `C:`, which is drive-RELATIVE -- it names the working
    directory on that drive, not the drive root -- so it collapses the same way an
    empty string does. Both forms are rejected."""
    body = INSTALLER_PS1.read_text()
    assert "-Prefix may not be a filesystem root" in body
    assert "-BinDir may not be a filesystem root" in body
    assert body.count("'^[A-Za-z]:$'") == 2, "both parameters must reject a bare drive"


@posix_only
def test_a_node_without_npm_bootstraps_instead_of_demanding_npm(
    tmp_path: Path, stubs: Path, node_mirror: Path
) -> None:
    """`apt install nodejs` on Debian and Ubuntu does NOT bring npm -- they are
    separate packages -- so a modern Node with no npm beside it is an ordinary
    configuration, not a broken one. Telling that user to install npm hands back
    exactly the prerequisite this installer exists to remove, so the reusable Node
    is abandoned and a private one that bundles npm is bootstrapped instead.

    Driven all the way to a working install rather than asserting on the message,
    because the point is that the user ends up with the tool.
    """
    base = _expected_node_base(TESTED_NODE_VERSION)
    archive = _node_tarball(tmp_path, base)
    shutil.copy(archive, node_mirror / archive.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (node_mirror / "SHASUMS256.txt").write_text(f"{digest}  {archive.name}\n")
    _fake_curl(stubs, node_mirror)
    _fake_node(stubs)  # a usable Node, with no npm anywhere beside it or on PATH

    result = _run(
        tmp_path,
        stubs,
        "--node-mirror",
        "https://node.internal.example/dist",
        "--node-version",
        TESTED_NODE_VERSION,
        isolated=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no npm beside it" in result.stdout, result.stdout
    assert "install npm" not in result.stdout + result.stderr
    # The private Node was used, and the tool is installed and runnable.
    assert (tmp_path / "datahome" / "playwright-cli" / "node" / "bin" / "npm").exists()
    assert (tmp_path / "home" / ".local" / "bin" / "playwright-cli").is_file()


def test_the_windows_installer_never_builds_a_cmd_command_line() -> None:
    """cmd.exe expands %VAR% while parsing a command line, so any path we embed in
    one can be rewritten before the target sees it -- and `%PATH%` is a legal
    directory name on NTFS. The install therefore runs node against npm's own
    npm-cli.js, with no shell at any point, and the wrapper is invoked through the
    call operator so PowerShell owns the quoting.
    """
    body = INSTALLER_PS1.read_text()
    assert "cmd.exe /c" not in body, "a cmd.exe command line re-parses % in our paths"
    assert "npm-cli.js" in body, "the install must bypass npm.cmd when it can"
    assert "& $wrapper --version" in body


def test_the_generated_wrapper_disables_delayed_expansion() -> None:
    """A path may legally contain `!`. Invoked from a shell started with
    `cmd /V:ON`, delayed expansion is inherited and would eat the `!` and its
    neighbours out of the PATH line, so the wrapper turns it off for its own
    scope. `%ERRORLEVEL%` is ordinary expansion and is unaffected.
    """
    body = INSTALLER_PS1.read_text()
    wrapper = body[body.index("$wrapperBody = @") :]
    setlocal = wrapper.index("setlocal DisableDelayedExpansion")
    path_line = wrapper.index('set "PATH=')
    assert setlocal < path_line, "delayed expansion must be off before PATH is set"
    assert "exit /b %ERRORLEVEL%" in wrapper


def test_the_isolated_env_excludes_version_manager_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An isolated HOME is not enough. A version manager also finds its own data
    directory through its own variables, so an inherited `MISE_DATA_DIR` would let
    a shim provision into the real one no matter what HOME says. The environment is
    therefore built from an allowlist: a variable this suite has never heard of --
    the next version manager's -- is absent because it was never named, not
    because someone remembered to exclude it.
    """
    for name in (
        "MISE_DATA_DIR",
        "MISE_CONFIG_DIR",
        "ASDF_DATA_DIR",
        "ASDF_DIR",
        "NVM_DIR",
        "VOLTA_HOME",
        "PNPM_HOME",
        "N_PREFIX",
        "NODE_OPTIONS",
        "NPM_CONFIG_PREFIX",
        "SOME_FUTURE_TOOL_HOME",
    ):
        monkeypatch.setenv(name, "/real/outside/tmp_path")
    env = _isolated_tool_env(tmp_path)
    leaked = sorted(key for key, value in env.items() if value == "/real/outside/tmp_path")
    assert not leaked, f"inherited from the ambient environment: {leaked}"
    # PATH is the one thing that must survive, or nothing is findable.
    assert env["PATH"] == os.environ["PATH"]


@posix_only
@pytest.mark.parametrize("shape", ["prefix", "bin-dir"])
def test_a_path_separator_in_a_directory_is_refused(
    tmp_path: Path, stubs: Path, shape: str
) -> None:
    """`:` is legal in a directory name and is also PATH's separator, and PATH has
    no escaping mechanism -- so a bootstrapped Node under such a prefix would be
    prepended as two nonexistent entries and npm's `#!/usr/bin/env node` shim would
    fail to find the interpreter just installed. Nothing can be fixed at the point
    of use, so it is refused up front.
    """
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    hostile = str(tmp_path / "pw:cli")
    args = ["--prefix", hostile] if shape == "prefix" else ["--bin-dir", hostile]
    result = _run(tmp_path, stubs, "--version", "0.1.18", *args)
    assert result.returncode == EXIT_CODES["usage"], result.stdout + result.stderr
    assert "separates PATH entries" in result.stderr


@posix_only
def test_a_query_string_credential_is_redacted(tmp_path: Path, stubs: Path) -> None:
    """Not every registry authenticates with userinfo -- a mirror may take
    `?token=...` instead, and redacting only `//user:pass@` would print and persist
    that one verbatim. The whole query is replaced from its first `?` rather than
    by parameter name, because naming them would miss the next one.
    """
    secret = "tok-abcdef123456"
    registry = f"https://mirror.internal.example/npm/?access_token={secret}"
    _fake_node(stubs)
    _fake_npm_failing(stubs, f"npm error code E401\nnpm error 401 for {registry}")
    # Through the environment, which is where a credentialed registry now has to
    # come from: as a flag it would be world-readable in /proc and is refused.
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        extra_env={"KIROCREW_NPM_REGISTRY": registry},
    )
    assert result.returncode == EXIT_CODES["registry_auth"]
    log = (tmp_path / "datahome" / "playwright-cli" / "playwright-cli-install.log").read_text()
    for haystack in (result.stdout + result.stderr, log):
        assert secret not in haystack, haystack
    assert "?***" in log


def test_the_node_floor_matches_what_the_product_requires_of_this_cli() -> None:
    """These installers provision the CLI that Kiro Crew's own browsing drives
    (`browser_cli/install.py`, added when browsing moved off the MCP proxy). If
    they accepted an older Node than the product does, they would install a tool
    that works at the shell and is then refused by the app -- the worst shape of
    failure, because the install SUCCEEDED. The scripts cannot import the constant,
    running as they do before any Python exists, so it is bound here instead.
    """
    from kiro_crew.browser_cli.install import MIN_NODE_MAJOR as product_floor

    sh = re.search(r"^MIN_NODE_MAJOR=(\d+)", INSTALLER_SH.read_text(), re.MULTILINE)
    ps1 = re.search(r"^\$MinNodeMajor = (\d+)", INSTALLER_PS1.read_text(), re.MULTILINE)
    assert sh is not None and ps1 is not None
    assert int(sh.group(1)) == product_floor, (
        f"playwright-cli.sh accepts Node {sh.group(1)} but the product requires "
        f"{product_floor}; it would install a CLI the app refuses to drive"
    )
    assert int(ps1.group(1)) == product_floor, (
        f"playwright-cli.ps1 accepts Node {ps1.group(1)} but the product requires "
        f"{product_floor}"
    )


@posix_only
def test_a_second_question_mark_does_not_leave_the_credential_behind(
    tmp_path: Path, stubs: Path
) -> None:
    """The query match is anchored on the FIRST `?`. With `?` allowed in the
    captured prefix the match is greedy and runs to the LAST one on the line, so a
    URL carrying a second `?` -- a callback parameter, say -- kept its credential.
    """
    secret = "tok-second-question"
    registry = f"https://mirror.internal.example/npm/?token={secret}&cb=https://x/?y=1"
    _fake_node(stubs)
    _fake_npm_failing(stubs, f"npm error code E401\nnpm error 401 for {registry}")
    # Through the environment, which is where a credentialed registry now has to
    # come from: as a flag it would be world-readable in /proc and is refused.
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        extra_env={"KIROCREW_NPM_REGISTRY": registry},
    )
    assert result.returncode == EXIT_CODES["registry_auth"]
    log = (tmp_path / "datahome" / "playwright-cli" / "playwright-cli-install.log").read_text()
    for haystack in (result.stdout + result.stderr, log):
        assert secret not in haystack, haystack


@posix_only
def test_the_dry_run_plan_does_not_promise_a_reuse_it_will_not_make(
    tmp_path: Path, stubs: Path, node_mirror: Path
) -> None:
    """A Node with no npm beside it is abandoned by the real run, so a plan that
    says `reuse` describes an install that will not happen -- and the plan exists
    precisely so the user can check before committing. The downloader stub is
    needed because the plan now reaches the bootstrap branch, which asserts up
    front that a download is possible at all.
    """
    _fake_node(stubs)  # usable Node, no npm anywhere
    _fake_curl(stubs, node_mirror)
    # isolated: the default PATH keeps the host's real npm, which would be found
    # and would make the plan legitimately say "reuse".
    result = _run(tmp_path, stubs, "--version", "0.1.18", "--dry-run", isolated=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reuse" not in result.stdout, result.stdout
    assert "no npm beside it" in result.stdout, result.stdout


@posix_only
def test_browsers_are_fetched_at_install_time_not_first_use(tmp_path: Path, stubs: Path) -> None:
    """The browser comes from the Playwright CDN, not the npm registry, so a network
    that allows npm can still block it. Fetching it here is what turns that into a
    classified failure with a mirror remedy instead of a stall inside the user's
    first browse.
    """
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(tmp_path, stubs, "--version", "0.1.18")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "downloading browser binaries" in result.stdout
    marker = tmp_path / "datahome" / "playwright-cli" / "install-browser-called"
    assert marker.exists(), "install-browser must run under the classified environment"


@posix_only
def test_skip_browsers_still_skips_the_download(tmp_path: Path, stubs: Path) -> None:
    """`--skip-browsers` is the documented escape for an air-gapped CDN, so the new
    install-time fetch must honour it."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(tmp_path, stubs, "--version", "0.1.18", "--skip-browsers")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "downloading browser binaries" not in result.stdout
    assert not (tmp_path / "datahome" / "playwright-cli" / "install-browser-called").exists()


def test_both_installers_reject_a_reparse_point_before_writing_the_wrapper() -> None:
    """`GetFullPath` is lexical and never follows a junction, so a -BinDir junction
    pointing at the prefix compares as different while resolving to the same
    directory -- and the wrapper would overwrite the npm shim it calls, recursing
    until the stack ran out. 5.1 cannot resolve a junction target without P/Invoke,
    so it is refused; the shell installer gets this from `cd -P`.
    """
    ps1 = INSTALLER_PS1.read_text()
    # Every ANCESTOR, not just the leaf: a junction one level up aliases the pair
    # while both leaves look ordinary and the strings differ.
    assert "function Find-ReparsePointAncestor" in ps1
    assert "Split-Path -Parent $current" in ps1, "the walk must climb to the root"
    # The shell installer needs no equivalent: `cd -P` resolves every component.
    assert 'cd -P -- "$PREFIX"' in INSTALLER_SH.read_text()


@posix_only
def test_a_blocked_browser_cdn_exits_16_with_a_mirror_remedy(tmp_path: Path, stubs: Path) -> None:
    """The failure branch is the entire reason the browser fetch moved into the
    install, so it needs a test of its own -- the success-path tests above pass
    whether or not this branch works at all. It shipped once referring to an
    exit-code constant that was never defined, which under `set -eu` aborted with
    'unbound variable' instead of printing the remedy.
    """
    _fake_node(stubs)
    _fake_npm_succeeding(
        stubs,
        browser_failure="Download failure: failed to download chromium, "
        "connect ETIMEDOUT cdn.playwright.dev",
    )
    result = _run(tmp_path, stubs, "--version", "0.1.18")
    assert result.returncode == EXIT_CODES["browser_download"], result.stdout + result.stderr
    assert "unbound variable" not in result.stderr, result.stderr
    # The remedy must actually reach the user: both escapes, named.
    assert "--download-host" in result.stderr, result.stderr
    assert "--skip-browsers" in result.stderr, result.stderr
    # The CLI itself stays installed -- only the browser is missing.
    assert (tmp_path / "home" / ".local" / "bin" / "playwright-cli").is_file()


@posix_only
def test_a_credentialed_download_host_is_scrubbed_after_a_SUCCESSFUL_install(
    tmp_path: Path, stubs: Path
) -> None:
    """The log outlives a successful install, and a success is exactly when nobody
    reads it again before attaching it to a bug report. Playwright echoes the
    download host it used, so sanitising only on failure leaves a credentialed
    mirror URL on disk in the one case the user thinks went fine.
    """
    secret = "cdn-token-987654"
    host = f"https://user:{secret}@playwright.internal.example/"
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    # Exported, not passed as a flag: as a flag the credential would sit in
    # world-readable argv and is refused. The installer passes an already-set
    # PLAYWRIGHT_DOWNLOAD_HOST through to npm untouched.
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        extra_env={"PLAYWRIGHT_DOWNLOAD_HOST": host},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    log = tmp_path / "datahome" / "playwright-cli" / "playwright-cli-install.log"
    assert secret not in log.read_text(), log.read_text()
    assert secret not in result.stdout + result.stderr
    # The host still reached the CLI: it is the LOG that is scrubbed, not the run.
    recorded = tmp_path / "datahome" / "playwright-cli" / "install-browser-called"
    assert secret in recorded.read_text(), "the real host must still be passed through"


@posix_only
def test_replacing_a_bootstrapped_node_never_leaves_the_prefix_without_one(
    tmp_path: Path, stubs: Path, node_mirror: Path
) -> None:
    """The wrapper pins the bootstrapped Node's directory, so a window in which that
    directory does not exist is a window in which the user's installed CLI cannot
    run. Deleting the old tree before promoting the new one creates exactly that
    window, and it is reachable on an ordinary path: a stamped, runnable Node with
    no npm is re-bootstrapped rather than reused. The old tree is moved aside and
    only removed once the new one is in place.
    """
    base = _expected_node_base(TESTED_NODE_VERSION)
    archive = _node_tarball(tmp_path, base)
    shutil.copy(archive, node_mirror / archive.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (node_mirror / "SHASUMS256.txt").write_text(f"{digest}  {archive.name}\n")
    _fake_curl(stubs, node_mirror)

    # A previous bootstrap: stamped, runnable, but with npm removed -- which is what
    # sends this run back through the bootstrap over the top of it.
    node_dir = tmp_path / "datahome" / "playwright-cli" / "node"
    (node_dir / "bin").mkdir(parents=True)
    _write_stub(node_dir / "bin" / "node", "#!/bin/sh\necho 22.0.0\nexit 0\n")
    (node_dir / ".kirocrew-playwright-cli-node").write_text("")

    result = _run(
        tmp_path,
        stubs,
        "--node-mirror",
        "https://node.internal.example/dist",
        "--node-version",
        TESTED_NODE_VERSION,
        isolated=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (node_dir / "bin" / "npm").exists(), "the replacement tree must be in place"
    # No leftovers: neither the staging directory nor the moved-aside backup.
    leftovers = [
        p.name
        for p in (tmp_path / "datahome" / "playwright-cli").iterdir()
        if p.name.startswith("node.incoming")
    ]
    assert leftovers == [], leftovers


def test_both_installers_move_the_old_node_aside_rather_than_deleting_it() -> None:
    """Asserted structurally as well, because the interruption itself cannot be
    staged from a test: the failure needs the process to die between two syscalls.

    Positional, not absolute. Removing the target is legitimate AFTER promotion --
    that is the rollback discarding a Node that turned out not to run here -- and
    illegitimate BEFORE it, which is the window that loses a working install. An
    earlier version of this test forbade the removal outright and then failed on the
    rollback it was supposed to protect.
    """
    sh = INSTALLER_SH.read_text()
    aside = sh.index('mv "$PREFIX/node" "$_backup"')
    promote = sh.index('mv "$_stage/tree" "$PREFIX/node"')
    assert aside < promote, "the old tree must be moved aside before promotion"
    for match in re.finditer(r'rm -rf -- "\$PREFIX/node"', sh):
        assert match.start() > promote, (
            "removing the target before promotion is the window that breaks a " "working install"
        )

    ps1 = INSTALLER_PS1.read_text()
    ps_aside = ps1.index('$backup = Join-Path $staging "previous"')
    ps_promote = ps1.index("Move-Item -LiteralPath $extracted -Destination $target")
    assert ps_aside < ps_promote
    for match in re.finditer(r"Remove-Item -LiteralPath \$target -Recurse", ps1):
        assert match.start() > ps_promote


@posix_only
def test_a_node_that_does_not_run_here_leaves_the_previous_one_in_place(
    tmp_path: Path, stubs: Path, node_mirror: Path
) -> None:
    """A checksum-valid archive can still hold a Node that does not run on THIS host
    -- wrong libc, wrong architecture inside a correctly named tarball. The rollback
    therefore has to outlive the runtime probe, or that input trades a working
    install for a broken one: the wrapper pins this directory.
    """
    base = _expected_node_base(TESTED_NODE_VERSION)
    # An archive whose `node` is present, executable, and fails to run.
    tree = tmp_path / "badsrc" / base / "bin"
    tree.mkdir(parents=True)
    _write_stub(tree / "node", "#!/bin/sh\nexit 127\n")
    _write_stub(tree / "npm", "#!/bin/sh\nexit 0\n")
    archive = tmp_path / f"{base}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(tmp_path / "badsrc" / base, arcname=base)
    shutil.copy(archive, node_mirror / archive.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (node_mirror / "SHASUMS256.txt").write_text(f"{digest}  {archive.name}\n")
    _fake_curl(stubs, node_mirror)

    # A previous bootstrap worth keeping: stamped, runnable, npm removed so this run
    # re-bootstraps over it.
    node_dir = tmp_path / "datahome" / "playwright-cli" / "node"
    (node_dir / "bin").mkdir(parents=True)
    _write_stub(node_dir / "bin" / "node", "#!/bin/sh\necho 22.0.0\nexit 0\n")
    (node_dir / ".kirocrew-playwright-cli-node").write_text("")

    result = _run(
        tmp_path,
        stubs,
        "--node-mirror",
        "https://node.internal.example/dist",
        "--node-version",
        TESTED_NODE_VERSION,
        isolated=True,
    )
    assert result.returncode == EXIT_CODES["node_bootstrap"], result.stdout + result.stderr
    # The previous interpreter survived, so anything already pinned to it still runs.
    assert (node_dir / "bin" / "node").exists(), "the previous Node must be restored"
    assert (
        "0"
        == subprocess.run(  # noqa: S603 - the restored stub, not a real tool
            ["sh", "-c", f'"{node_dir}/bin/node" >/dev/null 2>&1; echo $?'],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    )


@posix_only
def test_an_interrupted_rebootstrap_restores_the_previous_node(
    tmp_path: Path, stubs: Path, node_mirror: Path
) -> None:
    """SIGINT during promotion is the realistic interruption -- a user pressing
    Ctrl-C -- and it lands in the window where the old tree has been moved aside and
    the new one is not yet in place. The EXIT handler puts it back, so the wrapper
    never ends up pinned to a Node that is no longer there.

    Driven by making the tarball enormous enough that `tar` is still unpacking when
    the signal arrives, rather than by asserting on the script's text.
    """
    base = _expected_node_base(TESTED_NODE_VERSION)
    tree = tmp_path / "slowsrc" / base / "bin"
    tree.mkdir(parents=True)
    _write_stub(tree / "node", "#!/bin/sh\necho 22.0.0\nexit 0\n")
    _write_stub(tree / "npm", "#!/bin/sh\nexit 0\n")
    # ~60 MB of incompressible filler, so the unpack takes long enough to interrupt.
    (tmp_path / "slowsrc" / base / "filler").write_bytes(os.urandom(60 * 1024 * 1024))
    archive = tmp_path / f"{base}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(tmp_path / "slowsrc" / base, arcname=base)
    shutil.copy(archive, node_mirror / archive.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (node_mirror / "SHASUMS256.txt").write_text(f"{digest}  {archive.name}\n")
    _fake_curl(stubs, node_mirror)

    node_dir = tmp_path / "datahome" / "playwright-cli" / "node"
    (node_dir / "bin").mkdir(parents=True)
    _write_stub(node_dir / "bin" / "node", "#!/bin/sh\necho 22.0.0\nexit 0\n")
    (node_dir / ".kirocrew-playwright-cli-node").write_text("")
    sentinel = node_dir / "bin" / "sentinel"
    sentinel.write_text("the tree that must survive")

    env = _env(tmp_path, stubs, isolated=True)
    proc = subprocess.Popen(  # noqa: S603 - the installer under test
        [
            "sh",
            str(INSTALLER_SH),
            "--node-mirror",
            "https://node.internal.example/dist",
            "--node-version",
            TESTED_NODE_VERSION,
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        # Long enough to be inside the bootstrap, short enough to be mid-unpack.
        time.sleep(2.5)
        os.killpg(proc.pid, signal.SIGINT)
        proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()

    # Whatever stage it died in, a Node must be present and it must be the old one
    # if promotion never happened. The one thing forbidden is: nothing there.
    assert node_dir.exists(), "the interrupt left the prefix with no Node at all"
    assert (node_dir / "bin" / "node").exists()


@posix_only
def test_a_credentialed_node_mirror_is_refused_not_merely_redacted(
    tmp_path: Path, stubs: Path
) -> None:
    """Redaction protects what the script PRINTS. It cannot protect what the kernel
    publishes: the mirror URL is passed to curl as an argument, and
    /proc/<pid>/cmdline is world-readable, so any other account on the host can read
    the token for the duration of the download. The registry URL is a different
    case and stays supported -- it travels in the environment, and
    /proc/<pid>/environ is readable only by its owner.
    """
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    secret = "mirror-token-abc"
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        "--node-mirror",
        f"https://user:{secret}@node.internal.example/dist",
        "--dry-run",
    )
    assert result.returncode == EXIT_CODES["usage"], result.stdout + result.stderr
    assert "may not embed a credential" in result.stderr
    assert secret not in result.stdout + result.stderr, "the refusal must not echo it"


@posix_only
def test_an_at_sign_in_a_mirror_path_is_not_mistaken_for_a_credential(
    tmp_path: Path, stubs: Path
) -> None:
    """Only the authority carries userinfo. An `@` further along the path is
    ordinary, and refusing it would reject a legitimate mirror."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        "--node-mirror",
        "https://node.internal.example/dist/@current",
        "--dry-run",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_windows_install_command_cannot_run_a_stale_local_script() -> None:
    """`;` in PowerShell separates statements; it is not `&&`. A failed download
    would otherwise fall through to `powershell -File .\\playwright-cli.ps1` and
    execute whatever file of that name is already in the working directory -- stale,
    or planted by someone else. Measured on pwsh 7.6: `-ErrorAction Stop` alone does
    NOT prevent the next statement, because the terminating error ends the pipeline
    rather than the command. `&&` is unavailable, since this targets PowerShell 5.1.
    """
    import kiro_crew.browser_cli.install as install_mod
    from kiro_crew.browser_cli.install import _standalone_install_command

    original = install_mod.os.name
    try:
        install_mod.os.name = "nt"
        command = _standalone_install_command()
    finally:
        install_mod.os.name = original
    assert "try {" in command, command
    assert "-ErrorAction Stop" in command
    assert "catch { Write-Error $_; exit 1 }" in command
    # The guard has to sit BEFORE the execution, or it guards nothing.
    assert command.index("exit 1") < command.index("powershell -ExecutionPolicy")


@posix_only
def test_a_credential_that_is_not_url_shaped_is_scrubbed_too(tmp_path: Path, stubs: Path) -> None:
    """Redaction that only understands `//user:pass@host` misses the form npm
    actually echoes: an .npmrc assignment. `_authToken=`, `_password=`, `_auth=` and
    the NPM_TOKEN environment form all reach the log, which is kept on disk AND
    tailed to the terminal on failure, so a token in any of them outlives the run.
    """
    secret = "npmrc-token-abcdef"
    _fake_node(stubs)
    _fake_npm_failing(
        stubs,
        "npm error code E401\n"
        f"npm error config //npm.internal.example/:_authToken={secret}\n"
        f"npm error config _password={secret}-pw\n"
        f"npm error config _auth={secret}-basic\n"
        f"NPM_TOKEN={secret}-env\n"
        "npm error path /home/u/project",
    )
    result = _run(tmp_path, stubs, "--version", "0.1.18")
    assert result.returncode == EXIT_CODES["registry_auth"]
    log = (tmp_path / "datahome" / "playwright-cli" / "playwright-cli-install.log").read_text()
    for haystack in (result.stdout + result.stderr, log):
        assert secret not in haystack, haystack
    assert "_authToken=***" in log, log
    # Ordinary lines survive: a sanitiser that eats the diagnosis defeats the log.
    assert "npm error path /home/u/project" in log


def test_both_installers_probe_the_staged_node_before_promoting_it() -> None:
    """Ordering is the whole fix, and it is not observable from the end state.

    Promoting first and probing after leaves an interval in which an UNVALIDATED
    tree is in place while the old one waits in staging. An interrupt inside that
    interval runs the cleanup, which sees the target present, correctly declines to
    restore -- and then deletes the rollback, leaving an unvalidated Node with no way
    back. Four review rounds were spent guarding that interval before it became
    clear the interval should not exist: nothing under the prefix is touched until
    the downloaded bits are known to run.
    """
    sh = INSTALLER_SH.read_text()
    probe = sh.index('_try_node "$_stage/tree/bin/node"')
    aside = sh.index('mv "$PREFIX/node" "$_backup"')
    promote = sh.index('mv "$_stage/tree" "$PREFIX/node"')
    assert probe < aside < promote, "probe, then move aside, then promote"
    # The post-promotion probe is gone rather than merely reordered. Scoped to the
    # bootstrap: `_resolve_node` legitimately probes the installed location on a
    # LATER run, which is a different thing from probing a tree just promoted.
    bootstrap = sh[sh.index("_bootstrap_node() {") :]
    assert '_try_node "$PREFIX/node/bin/node"' not in bootstrap

    ps1 = INSTALLER_PS1.read_text()
    ps_probe = ps1.index('Try-Node (Join-Path $extracted "node.exe")')
    ps_promote = ps1.index("Move-Item -LiteralPath $extracted -Destination $target")
    assert ps_probe < ps_promote
    assert 'Try-Node (Join-Path $Prefix "node\\node.exe")' in ps1, (
        "Resolve-Node still probes the installed location on a LATER run, which is a "
        "different thing from probing a tree this run just promoted"
    )


@posix_only
@pytest.mark.parametrize("flag", ["--registry", "--download-host", "--node-mirror"])
def test_no_url_flag_accepts_a_credential(tmp_path: Path, stubs: Path, flag: str) -> None:
    """A credential passed as a FLAG is in this script's own argv, and
    /proc/<pid>/cmdline is world-readable -- so every account on the host can read it
    for the duration of the install, and it lands in shell history too.

    This supersedes an argument I made one round earlier, that `--registry` was safe
    because npm receives it through the environment. True, and beside the point: the
    value is on OUR command line before any child sees it.
    """
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    secret = f"flag-token-{flag.strip('-')}"
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        flag,
        f"https://user:{secret}@host.internal.example/",
        "--dry-run",
    )
    assert result.returncode == EXIT_CODES["usage"], result.stdout + result.stderr
    assert "may not embed a credential" in result.stderr
    assert secret not in result.stdout + result.stderr, "the refusal must not echo it"


@posix_only
def test_the_environment_route_still_accepts_a_credential(tmp_path: Path, stubs: Path) -> None:
    """Refusing the flag would be a capability regression if it left no way to reach
    an authenticated registry, and this PR exists for the enterprise case. The escape
    is the environment: readable only by its owner in /proc, unlike argv. It has to
    keep working, and the plan must still print the value REDACTED."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    secret = "env-token-987"
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        "--dry-run",
        extra_env={"KIROCREW_NPM_REGISTRY": f"https://u:{secret}@npm.internal.example/"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert secret not in result.stdout + result.stderr
    assert "//***@npm.internal.example" in result.stdout


@posix_only
@pytest.mark.parametrize("flag", ["--registry", "--download-host", "--node-mirror"])
def test_no_url_flag_accepts_a_query_string(tmp_path: Path, stubs: Path, flag: str) -> None:
    """`?token=` puts the secret in argv exactly as userinfo does, and the authority
    check alone let it through. Refused wholesale rather than by parameter name: the
    log sanitiser replaces an entire query for the same reason, because naming
    today's key would miss tomorrow's. These flags name a registry, a mirror and a
    CDN -- none needs a query, so refusing all of them costs nothing.
    """
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    secret = "query-token-55"
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        flag,
        f"https://host.internal.example/path?token={secret}",
        "--dry-run",
    )
    assert result.returncode == EXIT_CODES["usage"], result.stdout + result.stderr
    assert "may not carry a query string" in result.stderr
    assert secret not in result.stdout + result.stderr


@posix_only
def test_the_log_name_cannot_clobber_an_unrelated_file(tmp_path: Path, stubs: Path) -> None:
    """`--prefix` is caller-supplied, and the installer TRUNCATES its log before npm
    runs. A generic `install.log` therefore means `~/install.log` under
    `--prefix "$HOME"` -- an unrelated file, destroyed by an installer that never
    created it. The staging directory already avoided a predictable name for exactly
    this reason; the log did not.
    """
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    prefix = tmp_path / "home-like"
    prefix.mkdir()
    bystander = prefix / "install.log"
    bystander.write_text("someone else's log, which this installer must not touch\n")

    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        "--prefix",
        str(prefix),
        "--bin-dir",
        str(tmp_path / "bin"),
        "--skip-browsers",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert bystander.read_text().startswith("someone else's log"), "clobbered a bystander"
    assert (prefix / "playwright-cli-install.log").is_file(), "and must still keep its own"


# ── fail-closed download posture ─────────────────────────────────────


@posix_only
def test_a_wget_that_cannot_pin_https_is_refused(tmp_path: Path, stubs: Path) -> None:
    """BusyBox wget -- the default on Alpine, which is also the main musl target
    -- has no flag that refuses a redirect. Since the checksum MANIFEST travels
    that channel, a single 302 to http:// would let an attacker supply both the
    archive and the hash that blesses it, so the script refuses instead of
    verifying against a manifest it cannot trust."""
    _write_stub(
        stubs / "wget",
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  --help) echo "BusyBox v1.36 wget: -q -O FILE URL"; exit 0 ;;\n'
        "esac\n"
        "exit 1\n",
    )
    result = _run(tmp_path, stubs, "--dry-run", isolated=True)
    assert result.returncode == EXIT_CODES["missing_tool"]
    assert "--max-redirect" in result.stderr
    assert "curl" in result.stderr


@posix_only
def test_a_foreign_node_directory_is_never_deleted(
    tmp_path: Path, stubs: Path, node_mirror: Path
) -> None:
    """--prefix is caller-supplied, so $PREFIX/node can name a directory this
    installer never created -- `--prefix "$HOME"` on a machine with a ~/node
    source checkout, which the version probe rejects for having no bin/node, so
    the bootstrap proceeds. Deleting it would destroy the user's work."""
    base = _expected_node_base(TESTED_NODE_VERSION)
    archive = _node_tarball(tmp_path, base)
    shutil.copy(archive, node_mirror / archive.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (node_mirror / "SHASUMS256.txt").write_text(f"{digest}  {archive.name}\n")
    _fake_curl(stubs, node_mirror)

    prefix = tmp_path / "prefix"
    foreign = prefix / "node" / "src"
    foreign.mkdir(parents=True)
    (foreign / "keep.txt").write_text("the user's own work")

    result = _run(
        tmp_path,
        stubs,
        "--prefix",
        str(prefix),
        "--node-mirror",
        "https://node.internal.example/dist",
        "--node-version",
        TESTED_NODE_VERSION,
        isolated=True,
    )
    assert result.returncode == EXIT_CODES["node_bootstrap"]
    assert "not created by this installer" in result.stderr
    assert (foreign / "keep.txt").read_text() == "the user's own work"


@posix_only
def test_a_node_tree_this_installer_wrote_is_replaced(
    tmp_path: Path, stubs: Path, node_mirror: Path
) -> None:
    """The stamp guard must not block the ordinary re-bootstrap path, or a Node
    upgrade would need manual deletion."""
    base = _expected_node_base(TESTED_NODE_VERSION)
    archive = _node_tarball(tmp_path, base)
    shutil.copy(archive, node_mirror / archive.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (node_mirror / "SHASUMS256.txt").write_text(f"{digest}  {archive.name}\n")
    _fake_curl(stubs, node_mirror)

    args = (
        "--node-mirror",
        "https://node.internal.example/dist",
        "--node-version",
        TESTED_NODE_VERSION,
    )
    first = _run(tmp_path, stubs, *args, isolated=True)
    assert first.returncode == 0, first.stdout + first.stderr
    node_dir = tmp_path / "datahome" / "playwright-cli" / "node"
    assert (node_dir / ".kirocrew-playwright-cli-node").exists()

    # Remove the installed node so the second run must bootstrap again and
    # therefore must replace the stamped tree.
    (node_dir / "bin" / "node").unlink()
    second = _run(tmp_path, stubs, *args, isolated=True)
    assert second.returncode == 0, second.stdout + second.stderr
    assert (node_dir / "bin" / "node").exists()


@posix_only
def test_an_interrupted_pinned_install_repairs_itself(tmp_path: Path, stubs: Path) -> None:
    """An install interrupted after npm wrote package.json but before it created
    the global executable must not be skipped forever: without the executable
    check the retry short-circuits on the version alone and then fails
    verification every time, until the user discovers --force."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        # The manifest reports the pinned version, but no executable exists yet.
        extra_env={"PWCLI_FAKE_INSTALLED_VERSION": "0.1.18"},
    )
    assert result.returncode == 0, result.stderr
    assert "already installed" not in result.stdout
    assert (tmp_path / "datahome" / "playwright-cli" / "npm-args").exists()


# ── Windows script invariants (checked on every platform) ────────────


def test_powershell_survives_npm_writing_to_stderr() -> None:
    """npm writes to stderr on ORDINARY runs (deprecation notices). Under the
    script's global 'Stop' preference, Windows PowerShell 5.1 wraps merged native
    stderr as a NativeCommandError and promotes it to terminating -- which would
    abort before the failure classifier that is this installer's entire reason to
    exist, and skip writing the wrapper for a package that installed fine."""
    body = INSTALLER_PS1.read_text()
    installer = body.split("function Invoke-NpmInstall {", 1)[1].split("\n}", 1)[0]
    assert "$ErrorActionPreference = 'Continue'" in installer
    assert "catch {" in installer
    assert "$ErrorActionPreference = $previous" in installer
    # The version probe has the same hazard: a Node that greets on stderr would
    # be classed unusable and a second Node downloaded.
    probe = body.split("function Try-Node", 1)[1].split("\n}", 1)[0]
    assert "$ErrorActionPreference = 'Continue'" in probe


def test_the_generated_cmd_wrapper_is_written_for_cmd_exe() -> None:
    """A batch file is read in the OEM code page and re-expands %. ASCII output
    would turn C:\\Users\\Jose-with-an-accent into `?`, and an unescaped % would
    point the wrapper at a target that does not exist."""
    body = INSTALLER_PS1.read_text()
    assert "-replace '%', '%%'" in body
    assert "Set-Content" in body and "-Encoding OEM" in body
    assert "exit /b %ERRORLEVEL%" in body
    assert "[System.Text.Encoding]::ASCII" not in body


def test_windows_downloads_refuse_redirects() -> None:
    """Invoke-WebRequest otherwise follows up to five hops with no way to require
    that each stays HTTPS, and it is the checksum manifest travelling that
    channel.

    Asserted on the VALUE in force rather than on each call site's literal text:
    the previous form required `-MaximumRedirection 0` inline on every line and
    broke the moment the shared arguments moved into a splatted hashtable, even
    though the setting was still zero. The invariant is "no download follows a
    redirect", not "the flag is spelled inline".
    """
    body = INSTALLER_PS1.read_text()
    settings = re.findall(r"MaximumRedirection\s*(?:=|\s)\s*(\d+)", body)
    assert settings, "no MaximumRedirection setting found"
    assert set(settings) == {"0"}, f"a download may follow redirects: {settings}"
    calls = [
        line
        for line in body.splitlines()
        if "Invoke-WebRequest" in line and not line.lstrip().startswith("#")
    ]
    assert calls, "no Invoke-WebRequest calls found"
    for call in calls:
        assert "-MaximumRedirection 0" in call or "@webArgs" in call, call.strip()


def test_windows_node_staging_is_on_the_prefix_volume() -> None:
    """Move-Item cannot move a directory across volumes, so staging in %TEMP%
    aborts the bootstrap after the download was already verified whenever TEMP
    and the prefix sit on different drives."""
    body = INSTALLER_PS1.read_text()
    staging = [ln for ln in body.splitlines() if "$staging = " in ln]
    assert staging, "staging assignment not found"
    assert "GetTempPath" not in staging[0]
    assert "$Prefix" in staging[0]


def test_the_bootstrapped_node_version_agrees_across_both_scripts() -> None:
    """Three copies of this version exist (both scripts and this suite's
    fixtures) with nothing tying them together, so a bump in one place would
    leave the tests green while no longer exercising the shipped value."""
    sh_version = re.search(r'^NODE_VERSION="([0-9.]+)"', INSTALLER_SH.read_text(), re.MULTILINE)
    ps_version = re.search(
        r'^\s*\[string\]\$NodeVersion = "([0-9.]+)"',
        INSTALLER_PS1.read_text(),
        re.MULTILINE,
    )
    assert sh_version and ps_version
    assert sh_version.group(1) == ps_version.group(1)
    assert sh_version.group(1) == TESTED_NODE_VERSION


# ── npm's own output is untrusted text, not just ours ────────────────


@posix_only
def test_a_credential_in_npms_own_output_is_scrubbed_from_log_and_tail(
    tmp_path: Path, stubs: Path
) -> None:
    """Redacting only the values THIS script prints is not enough: npm echoes the
    resolved registry URL into its own output, which is captured to the log and
    then tailed to stderr on failure. The token would reach a terminal, a CI log,
    or a pasted bug report through that path."""
    _fake_node(stubs)
    _write_stub(
        stubs / "npm",
        "#!/bin/sh\n"
        'echo "npm error code E401" >&2\n'
        'echo "npm error GET $npm_config_registry - 401 Unauthorized" >&2\n'
        "exit 1\n",
    )
    secret = "s3cr3t-token-value"
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        extra_env={"KIROCREW_NPM_REGISTRY": f"https://bob:{secret}@npm.internal.example/"},
    )
    assert result.returncode == EXIT_CODES["registry_auth"]
    assert secret not in result.stdout + result.stderr
    # The file on disk is scrubbed too: it outlives the run, and a user reading or
    # attaching it to a bug report would otherwise expose the token.
    log = tmp_path / "datahome" / "playwright-cli" / "playwright-cli-install.log"
    body = log.read_text()
    assert secret not in body
    assert "//***@npm.internal.example" in body
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_the_windows_script_scrubs_its_log_too() -> None:
    """Same hazard, same obligation on the platform this suite cannot execute.

    The obligation is that the log is scrubbed before it is PRINTED. An earlier
    version of this test demanded it be scrubbed before it is CLASSIFIED, which is
    a different claim and the wrong one -- redaction deletes a log it cannot
    rewrite, so classifying afterwards can read nothing. See
    test_both_installers_classify_before_they_redact.
    """
    body = INSTALLER_PS1.read_text()
    assert "function Redact-Log" in body
    # Three call sites now: the failure path, the success path, and the
    # browser fetch on failure, and the browser fetch on success.
    assert body.count("Redact-Log $logPath") == 4
    failure_block = body.split("if (-not (Invoke-NpmInstall)) {", 1)[1]
    assert failure_block.index("Redact-Log $logPath") < failure_block.index(
        "Show-LogTail"
    ), "the log must be scrubbed before it is tailed to the user"


def test_windows_refuses_a_wrapper_it_cannot_represent() -> None:
    """A batch file is read in the console's OEM code page. A path character
    outside it is written as `?`, which would point the wrapper at a path that
    does not exist -- so the install fails closed instead of silently breaking."""
    body = INSTALLER_PS1.read_text()
    assert "OEMCodePage" in body
    assert "GetString($oemEncoding.GetBytes($wrapperBody))" in body
    assert "$ExVerify" in body.split("OEMCodePage", 1)[1][:800]


# ── paths that reach generated code ──────────────────────────────────


@posix_only
def test_a_trailing_newline_in_a_path_is_refused_up_front(tmp_path: Path, stubs: Path) -> None:
    """A TRAILING newline cannot survive the `dirname` round-trip (command
    substitution strips it), so the install would resolve to a different path and
    fail later as "the tool does not exist". Only that shape is refused."""
    _fake_node(stubs)
    result = _run(tmp_path, stubs, "--dry-run", "--prefix", f"{tmp_path}/trailing\n")
    assert result.returncode == EXIT_CODES["usage"]
    assert "may not end with a newline" in result.stderr


@posix_only
def test_an_embedded_newline_in_a_path_still_installs(tmp_path: Path, stubs: Path) -> None:
    """An embedded newline is legal on POSIX and DOES survive wrapper generation,
    so refusing it would remove a capability the escaping already handles."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    prefix = tmp_path / "em\nbedded"
    bindir = tmp_path / "bin"
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        "--prefix",
        str(prefix),
        "--bin-dir",
        str(bindir),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wrapper = bindir / "playwright-cli"
    # Bounded and given an isolated environment: this spawns the wrapper, which
    # spawns node, so an unbounded call leaves a process tree behind on hang.
    ran = run_bounded([str(wrapper), "--version"], env=_isolated_tool_env(tmp_path), timeout=120)
    assert ran.returncode == 0, ran.stderr
    assert "0.1.18" in ran.stdout


@posix_only
def test_an_ordinary_path_is_not_caught_by_the_newline_guard(tmp_path: Path, stubs: Path) -> None:
    """A `case` pattern built with $(printf '\\n') collapses to the empty string,
    which matches EVERY path. That mistake shipped once; this keeps it caught."""
    _fake_node(stubs)
    result = _run(tmp_path, stubs, "--dry-run", "--prefix", str(tmp_path / "plain"))
    assert result.returncode == 0, result.stderr
    assert "newline" not in result.stderr


@posix_only
def test_a_path_beginning_with_a_dash_is_a_path_not_an_option(tmp_path: Path, stubs: Path) -> None:
    """Without `--`, mkdir and rm read a leading-dash value as a flag bundle, so
    the install died on a legal directory name."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        "--prefix",
        str(tmp_path / "-dashpre"),
        "--bin-dir",
        str(tmp_path / "-dashbin"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "-dashbin" / "playwright-cli").exists()


# ── a downloader is only required where a download happens ───────────


@posix_only
def test_a_busybox_wget_host_that_already_has_node_still_installs(
    tmp_path: Path, stubs: Path
) -> None:
    """Refusing BusyBox wget at startup would turn a working install into a hard
    failure on a host that needs no download at all, so the refusal belongs at the
    point of download rather than at the door."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    _write_stub(
        stubs / "wget",
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  --help) echo "BusyBox v1.36 wget: -q -O FILE URL"; exit 0 ;;\n'
        "esac\n"
        "exit 1\n",
    )
    result = _run(tmp_path, stubs, "--version", "0.1.18", isolated=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "home" / ".local" / "bin" / "playwright-cli").exists()


@posix_only
def test_a_foreign_staging_directory_is_never_deleted(
    tmp_path: Path, stubs: Path, node_mirror: Path
) -> None:
    """The stamp guard protects $PREFIX/node, but a FIXED staging name was still
    cleared with `rm -rf` before it was created, destroying an unrelated
    directory of that name under a caller-supplied --prefix. Staging now gets a
    fresh mktemp name, which cannot collide with the user's own data."""
    base = _expected_node_base(TESTED_NODE_VERSION)
    archive = _node_tarball(tmp_path, base)
    shutil.copy(archive, node_mirror / archive.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (node_mirror / "SHASUMS256.txt").write_text(f"{digest}  {archive.name}\n")
    _fake_curl(stubs, node_mirror)

    prefix = tmp_path / "prefix"
    foreign = prefix / "node.incoming" / "src"
    foreign.mkdir(parents=True)
    (foreign / "keep.txt").write_text("the user's own work")

    result = _run(
        tmp_path,
        stubs,
        "--prefix",
        str(prefix),
        "--node-mirror",
        "https://node.internal.example/dist",
        "--node-version",
        TESTED_NODE_VERSION,
        isolated=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (foreign / "keep.txt").read_text() == "the user's own work"
    assert (prefix / "node" / "bin" / "node").exists()


@posix_only
def test_the_installers_own_mktemp_stays_inside_the_test_tmp_dir(
    tmp_path: Path, stubs: Path, node_mirror: Path
) -> None:
    """`no-test-side-effects` is a blocking rule: the Node bootstrap calls
    `mktemp -d`, which without TMPDIR writes into the HOST /tmp and leaves a tree
    that outlives the run."""
    base = _expected_node_base(TESTED_NODE_VERSION)
    archive = _node_tarball(tmp_path, base)
    shutil.copy(archive, node_mirror / archive.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (node_mirror / "SHASUMS256.txt").write_text(f"{digest}  {archive.name}\n")
    _fake_curl(stubs, node_mirror)

    host_tmp_before = set(Path("/tmp").glob("tmp.*"))
    result = _run(
        tmp_path,
        stubs,
        "--node-mirror",
        "https://node.internal.example/dist",
        "--node-version",
        TESTED_NODE_VERSION,
        isolated=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # mktemp's default template is tmp.XXXXXXXXXX; none may have appeared in the
    # host tmp dir during this run.
    assert not (set(Path("/tmp").glob("tmp.*")) - host_tmp_before)
    assert (tmp_path / "tmp").is_dir()


# ── printing a URL is what needs redaction, not one option ───────────

#: Variables holding a URL that a caller can supply, and which may therefore
#: carry userinfo (``https://user:token@host/``). A user-facing message must
#: interpolate the redacted companion, never these.
RAW_URL_VARS_SH = ("$REGISTRY", "$DOWNLOAD_HOST", "$NODE_ART_MIRROR", "$_url", "$_sums")
RAW_URL_VARS_PS = ("$Registry", "$DownloadHost", "$zipUrl", "$sumsUrl")


def test_no_user_facing_message_interpolates_a_raw_url() -> None:
    """A structural guard, not a spot check. Redacting the registry alone left
    --node-mirror and --download-host leaking through the bootstrap diagnostics
    and the --dry-run plan, because the fix was attached to one option instead of
    to the act of printing a URL. This enumerates the whole class."""
    emitters_sh = ("echo ", "say ", "warn ", "die ")
    offenders: list[str] = []

    def _message_part(text: str, emitters: tuple[str, ...]) -> str:
        """Only what FOLLOWS the emitter is a message. `_fetch "$_sums" || die
        "..."` names the raw URL as an argument to the fetch, which is correct."""
        positions = [text.index(e) for e in emitters if e in text]
        return text[min(positions) :] if positions else ""

    for line in INSTALLER_SH.read_text().splitlines():
        stripped = _message_part(line.strip(), emitters_sh)
        if not stripped or line.strip().startswith("#"):
            continue
        for var in RAW_URL_VARS_SH:
            # The negative lookahead is what lets the DISPLAY companion through:
            # $NODE_ART_MIRROR_DISPLAY must not read as a hit on $NODE_ART_MIRROR.
            if re.search(re.escape(var) + r"(?![A-Za-z0-9_])", stripped):
                offenders.append(f"sh: {stripped}")
    emitters_ps = ("Write-Host ", "Say ", "Warn ", "Die ")
    for line in INSTALLER_PS1.read_text().splitlines():
        stripped = _message_part(line.strip(), emitters_ps)
        if not stripped or line.strip().startswith("#"):
            continue
        for var in RAW_URL_VARS_PS:
            if re.search(re.escape(var) + r"(?![A-Za-z0-9_])", stripped):
                # Redact-Url applied inline is the sanctioned form.
                if "Redact-Url" in stripped:
                    continue
                offenders.append(f"ps1: {stripped}")
    assert not offenders, "raw URL reached a user-facing message:\n" + "\n".join(offenders)


@posix_only
def test_an_authenticated_node_mirror_stays_possible_without_a_url_credential() -> None:
    """Refusing a credential inside `--node-mirror` removes a capability an internal
    mirror may genuinely need, so the refusal only holds up if the alternative it
    names actually works. It does: curl is invoked with `--netrc-optional`, and wget
    reads that file by default -- wget's own manual gives this exact advice, that a
    password on the command line "reveals your password to anyone who bothers to run
    ps" and belongs in a credentials file instead.

    This replaces a test that asserted the opposite contract -- that a credentialed
    mirror is ACCEPTED and merely redacted in diagnostics. Redaction covers what the
    script prints; it cannot cover what the kernel publishes about a child process.
    """
    body = INSTALLER_SH.read_text()
    assert "--netrc-optional" in body, "the documented escape has to be wired up"
    assert (
        "credentials file that curl and wget read" in body
    ), "and the refusal has to name the alternative"


@posix_only
def test_the_dry_run_plan_redacts_every_url_it_prints(tmp_path: Path, stubs: Path) -> None:
    """The plan exists to be read and pasted, which makes it the likeliest place
    for a token to be copied somewhere permanent. A Node below the floor is used
    so the plan also reports the mirror URL it would fetch from.

    The mirror carries no credential here, because one is now refused outright: it
    would reach curl as an argv entry, which redaction cannot reach. Registry and
    download-host still carry theirs -- both travel in the environment, where
    /proc/<pid>/environ is readable only by its owner -- so redaction remains their
    protection and this test remains their coverage.
    """
    _fake_node(stubs)
    result = _run(
        tmp_path,
        stubs,
        "--dry-run",
        "--node-mirror",
        "https://node.internal.example/dist",
        extra_env={
            "PWCLI_FAKE_NODE_MAJOR": "16",
            # Both credentials now arrive by environment: as flags they would sit in
            # world-readable argv and be refused.
            "KIROCREW_NPM_REGISTRY": "https://ru:reg-token-1@npm.internal.example/",
            "PLAYWRIGHT_DOWNLOAD_HOST": "https://du:cdn-token-3@cdn.internal.example/",
        },
    )
    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    for secret in ("reg-token-1", "cdn-token-3"):
        assert secret not in combined, secret
    # The registry is the one the PLAN prints. An exported PLAYWRIGHT_DOWNLOAD_HOST
    # is passed through to npm without the installer parsing it, so there is nothing
    # of it to redact here -- which is why its secret is asserted absent above rather
    # than expected in redacted form.
    assert combined.count("//***@") >= 1


@posix_only
def test_a_log_that_cannot_be_scrubbed_is_discarded_not_printed(
    tmp_path: Path, stubs: Path
) -> None:
    """Sanitization that fails OPEN defeats itself: the raw token-bearing npm log
    was still left on disk for the failure tail to print. With no working `sed`
    the log must be emptied and the tail suppressed instead."""
    _fake_node(stubs)
    secret = "leak-me-token"
    _write_stub(
        stubs / "npm",
        "#!/bin/sh\n"
        'echo "npm error code E401" >&2\n'
        'echo "npm error GET $npm_config_registry" >&2\n'
        "exit 1\n",
    )
    # A `sed` that cannot run is the reachable stand-in for a full filesystem or
    # an unreadable log -- both make the redaction filter fail.
    _write_stub(stubs / "sed", "#!/bin/sh\nexit 1\n")
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        extra_env={"KIROCREW_NPM_REGISTRY": f"https://bob:{secret}@npm.internal.example/"},
        isolated=True,
    )
    assert result.returncode == EXIT_CODES["registry_auth"]
    combined = result.stdout + result.stderr
    assert secret not in combined
    assert "could not be scrubbed" in combined
    log = tmp_path / "datahome" / "playwright-cli" / "playwright-cli-install.log"
    if log.exists():
        assert secret not in log.read_text()


@posix_only
@pytest.mark.parametrize("flag", ["--registry", "--node-mirror", "--download-host"])
def test_a_rejected_url_is_redacted_in_the_validation_error(
    tmp_path: Path, stubs: Path, flag: str
) -> None:
    """The https:// validator prints the value it rejected, and a mistyped scheme
    on a credentialed URL is a completely ordinary typo -- so the error itself is
    a leak path. A name-based guard missed this because the validator prints a
    positional parameter, which is why this one is behavioural."""
    secret = "typo-scheme-token"
    result = _run(tmp_path, stubs, flag, f"http://svc:{secret}@host.internal.example/")
    assert result.returncode == EXIT_CODES["usage"]
    assert secret not in result.stdout + result.stderr
    assert "svc" not in result.stdout + result.stderr
    assert "must be an https:// URL" in result.stderr


@posix_only
def test_a_bin_dir_equal_to_the_package_bin_dir_is_refused(tmp_path: Path, stubs: Path) -> None:
    """--bin-dir "$PREFIX/bin" makes the wrapper and its target the same file, so
    the wrapper execs itself and verification spins until the stack runs out."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    prefix = tmp_path / "prefix"
    result = _run(
        tmp_path,
        stubs,
        "--version",
        "0.1.18",
        "--prefix",
        str(prefix),
        "--bin-dir",
        str(prefix / "bin"),
    )
    assert result.returncode == EXIT_CODES["usage"]
    assert "own bin directory" in result.stderr


def test_every_windows_native_call_tolerates_stderr() -> None:
    """The global 'Stop' preference turns a native command's stderr into a
    terminating error on PowerShell 5.1, so a Node that merely prints a startup
    warning would abort the install. One helper covers every call site rather
    than each being wrapped by hand and one being forgotten."""
    body = INSTALLER_PS1.read_text()
    assert "function Invoke-Native" in body
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "& $" not in stripped:
            continue
        if "$Action" in stripped:  # the helper's own invocation
            continue
        assert (
            "Invoke-Native" in stripped or "$ErrorActionPreference = 'Continue'" in body
        ), f"unguarded native call: {stripped}"


def test_the_windows_wrapper_cannot_replace_its_own_target() -> None:
    body = INSTALLER_PS1.read_text()
    assert "GetFullPath($wrapper) -eq [System.IO.Path]::GetFullPath($target)" in body


def test_every_powershell_function_is_defined_before_it_is_called() -> None:
    """PowerShell resolves functions at CALL time, walking the script top-down, so
    a helper defined below its first use is a runtime crash rather than a parse
    error — and it replaces the honest usage error with an opaque exit 1.

    This is the class behind moving the redaction helper above the https
    validator: the shell twin was reordered and this one was not, so a
    lockstep check is the only thing that catches the asymmetry.
    """
    lines = INSTALLER_PS1.read_text().splitlines()
    definitions: dict[str, int] = {}
    for number, line in enumerate(lines, start=1):
        match = re.match(r"\s*function\s+([A-Za-z][\w-]*)", line)
        if match:
            definitions.setdefault(match.group(1), number)
    assert definitions, "no function definitions found"

    late: list[str] = []
    for name, defined_at in definitions.items():
        for number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("#") or re.match(r"\s*function\s", line):
                continue
            # A call is the bare name in a command position or interpolation.
            if not re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", stripped):
                continue
            if number < defined_at:
                late.append(f"{name} defined at line {defined_at}, called at {number}")
            break
    assert not late, "PowerShell function used before definition:\n" + "\n".join(late)


def test_an_empty_node_marker_does_not_crash_windows_resolution() -> None:
    """`Get-Content -TotalCount 1` on an empty file returns $null, and `.Trim()`
    on it throws — which under the global 'Stop' preference aborts before PATH or
    the bootstrap is even considered. An empty marker is an ordinary artifact of
    an interrupted `ensure-node.sh`."""
    body = INSTALLER_PS1.read_text()
    assert "([string](Get-Content -LiteralPath $marker -TotalCount 1)).Trim()" in body


@posix_only
def test_an_empty_node_marker_is_ignored_on_posix(tmp_path: Path, stubs: Path) -> None:
    """The shell twin of the above: an empty or whitespace marker must fall
    through to the next candidate rather than resolving to a bare '/node'."""
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    data_home = tmp_path / "datahome"
    data_home.mkdir(parents=True, exist_ok=True)
    (data_home / "node-bin-dir").write_text("")
    result = _run(tmp_path, stubs, "--version", "0.1.18")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "home" / ".local" / "bin" / "playwright-cli").exists()


@posix_only
@pytest.mark.skipif(
    shutil.which("npm") is None, reason="no real npm to validate the config files with"
)
def test_real_npm_accepts_the_isolated_config_files(tmp_path: Path, stubs: Path) -> None:
    """--isolated-npmrc is THE documented remedy for the enterprise auth failure,
    so it has to work against real npm, not just against a stub. npm rejects one
    path used as two scopes, so the two empty files must be distinct — and only
    real npm can confirm that. `config get` needs no network.
    """
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(tmp_path, stubs, "--version", "0.1.18", "--isolated-npmrc")
    assert result.returncode == 0, result.stdout + result.stderr

    isolated = tmp_path / "datahome" / "playwright-cli" / "isolated-npmrc"
    user, global_ = isolated / "user", isolated / "global"
    assert user.is_file() and global_.is_file()
    assert user != global_

    # HOME stays isolated. A version-manager shim (mise/asdf/nvm) resolves its
    # toolchain through HOME, so under an isolated one it either fails or blocks
    # trying to provision a toolchain that is not there -- on this host it hangs
    # rather than exiting. Both outcomes SKIP: letting the shim see the real HOME
    # would let it provision files outside tmp_path, which is the side effect this
    # suite exists to prevent. Where npm is a standalone binary (CI), the probe
    # runs and the contract below is genuinely enforced.
    env = _isolated_tool_env(
        tmp_path,
        npm_config_userconfig=str(user),
        npm_config_globalconfig=str(global_),
    )

    def _npm(*args: str, timeout: float) -> subprocess.CompletedProcess[str] | None:
        """None means npm never got far enough to answer: a shim that blocks or
        cannot start is indistinguishable from one that is merely slow.

        Bounded through `run_bounded`, not `subprocess.run(timeout=...)`: the
        latter kills only the direct child, so a version-manager shim that is
        mid-provision leaves its downloader running -- writing outside tmp_path,
        which is the whole hazard this probe is trying not to create.
        """
        try:
            return run_bounded(["npm", *args], env=env, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return None

    # Preflight, so a shimmed npm costs five seconds rather than the full probe
    # timeout on every developer machine: `npm --version` answers instantly when
    # npm can run at all, and blocks exactly when the shim would have to provision.
    if _npm("--version", timeout=5) is None:
        pytest.skip("npm cannot run under an isolated HOME on this host (version-manager shim)")
    probe = _npm("config", "get", "registry", "cache", "logs-dir", timeout=60)
    if probe is None:
        pytest.skip("npm did not answer `config get` under an isolated HOME")
    combined = probe.stdout + probe.stderr
    # The one outcome that is a real failure rather than an unusable npm: npm
    # refusing the two config files. Anything else means npm never got far enough
    # to judge them, which this host cannot distinguish from a broken shim.
    assert "double-loading config" not in combined, combined
    if probe.returncode != 0:
        pytest.skip(f"npm could not resolve a toolchain under an isolated HOME: {combined}")
    # The point of the isolation: npm's write targets resolve inside tmp_path, so
    # the run cannot deposit a cache or a debug log in the developer's real HOME.
    assert str(tmp_path) in probe.stdout, probe.stdout


def test_the_isolated_env_contains_every_write_target(tmp_path: Path) -> None:
    """The constructor's promise, asserted rather than described: HOME and every
    npm write target land inside tmp_path. A regression here is silent -- a tool
    would simply start writing to the operator's home -- so it is checked on the
    returned mapping instead of by reading the source, and there is deliberately
    no parameter that turns any of it off.
    """
    env = _isolated_tool_env(tmp_path)
    for key in (
        "HOME",
        "USERPROFILE",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "npm_config_cache",
        "npm_config_logs_dir",
    ):
        assert env[key].startswith(str(tmp_path)), f"{key}={env[key]} escapes {tmp_path}"
    assert env["HOME"] != os.environ.get("HOME"), "HOME was not isolated"
