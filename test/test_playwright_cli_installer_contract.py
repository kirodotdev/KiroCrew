"""The Playwright CLI installers' syntax, CLI contract and npm failure classification.

Both scripts parse under the shells they target, every accepted flag is documented,
an unknown argument or a plain-http URL is a usage error, and each npm failure a
corporate network produces is classified with the remedy for that case. Shared
stubs, environments and constants live in ``playwright_cli_installer_helpers.py``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from installer_test_helpers import run_bounded
from playwright_cli_installer_helpers import (  # noqa: F401 -- fixtures are used by name
    EXIT_CODES,
    INSTALLER_PS1,
    INSTALLER_SH,
    _fake_node,
    _fake_npm_failing,
    _isolated_tool_env,
    _native_tool_on_path,
    _run,
    _usable_powershell,
    posix_only,
    stubs_fixture,
)

# ── syntax ───────────────────────────────────────────────────────────


@posix_only
def test_installer_parses_as_posix_shell(tmp_path: Path) -> None:
    """The documented entry point is `curl … | sh`, so the script must parse
    under a plain POSIX shell and not only under bash."""
    subprocess.run(["sh", "-n", str(INSTALLER_SH)], check=True, cwd=str(tmp_path))


@posix_only
@pytest.mark.skipif(shutil.which("ksh") is None, reason="no non-bash POSIX shell available here")
def test_installer_parses_under_a_non_bash_shell(tmp_path: Path) -> None:
    """On most Linux distributions /bin/sh IS bash, so `sh -n` above happily
    accepts bashisms that dash and BusyBox ash reject. ksh is a genuinely
    different POSIX implementation, so parsing there catches what `sh -n` cannot
    on a bash-provided /bin/sh."""
    subprocess.run(["ksh", "-n", str(INSTALLER_SH)], check=True, cwd=str(tmp_path))


def test_installer_avoids_bash_only_syntax() -> None:
    """`sh -n` on a bash-provided /bin/sh accepts bashisms that dash rejects, so
    the constructs that actually break on Alpine/Debian are checked directly."""
    # Whole-line comments are stripped first: the prose explaining WHY a
    # construct is avoided naturally names it, and matching that would make the
    # check fire on its own rationale.
    body = "\n".join(
        line
        for line in INSTALLER_SH.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
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


@pytest.mark.skipif(
    _native_tool_on_path("pwsh") is None and _native_tool_on_path("powershell") is None,
    reason="no native PowerShell binary on PATH to parse playwright-cli.ps1",
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
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ── contract: help, flags, exit codes ────────────────────────────────


@posix_only
def test_help_documents_every_accepted_flag(tmp_path: Path, stubs: Path) -> None:
    """`--help` is the only documentation a piped-in installer can offer, so an
    added flag that never reaches the help text is a silent feature."""
    body = INSTALLER_SH.read_text(encoding="utf-8")
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
