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

This module pins enterprise credential hygiene, the fail-closed download posture, the Windows
script invariants, npm's output as untrusted text, the paths that reach generated code, and
where a downloader is required. The syntax, the CLI contract and the npm failure
classification live in ``test_playwright_cli_installer_contract.py``, the success path, the
Node bootstrap and the generated wrapper in ``test_playwright_cli_installer_install.py``, and
URL redaction in every message in ``test_playwright_cli_installer_url_redaction.py``; the
shared stubs, environments and constants in ``playwright_cli_installer_helpers.py``.
"""

from __future__ import annotations

import hashlib
import os
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
from playwright_cli_installer_helpers import (  # noqa: F401 -- fixtures are used by name
    EXIT_CODES,
    INSTALLER_PS1,
    INSTALLER_SH,
    TESTED_NODE_VERSION,
    _env,
    _expected_node_base,
    _fake_curl,
    _fake_node,
    _fake_npm_failing,
    _fake_npm_succeeding,
    _isolated_tool_env,
    _native_tool_on_path,
    _node_tarball,
    _run,
    _usable_powershell,
    _write_stub,
    node_mirror_fixture,
    posix_only,
    stubs_fixture,
)

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
    assert "LOG_SUPPRESSED=1" in INSTALLER_SH.read_text(encoding="utf-8")
    ps1 = INSTALLER_PS1.read_text(encoding="utf-8")
    assert "$script:LogSuppressed = $true" in ps1
    tail = ps1[ps1.index("function Show-LogTail") :]
    guard = tail.index("$script:LogSuppressed")
    printer = tail.index("-Tail 20")
    assert guard < printer, "Show-LogTail must consult the flag before printing the log"


def test_every_not_writable_handler_in_the_windows_installer_reports_the_cause() -> None:
    """Exit 17 is a classification, not a diagnosis. A handler that catches the
    exception and prints only its own sentence hides the one fact the user needs:
    a `Set-Acl` refused by a broken domain trust reads as "cannot restrict the
    install log to owner-only", which sends them chasing permissions. Every
    `catch` whose remedy is `Die $ExNotWritable` must carry the exception text.
    """
    ps1 = INSTALLER_PS1.read_text(encoding="utf-8")
    handlers = [
        match.group("body")
        for match in re.finditer(r"\}\s*catch\s*\{(?P<body>.*?)\n\s*\}", ps1, re.DOTALL)
        if "Die $ExNotWritable" in match.group("body")
    ]
    assert len(handlers) >= 5, "the handler scan no longer finds the exit-17 catch blocks"
    silent = [body.strip() for body in handlers if "$_.Exception.Message" not in body]
    assert not silent, f"exit-17 handlers that drop the underlying exception: {silent}"


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
def test_redaction_does_not_touch_an_at_sign_in_a_path(tmp_path: Path) -> None:
    """The greedy match must stay inside the authority: npm prints scoped package
    paths, and rewriting one would corrupt the log it exists to make readable.
    """
    script = INSTALLER_SH.read_text(encoding="utf-8")
    body = script[script.index("_redact_urls() {") :]
    assert "[^/[:space:]]*@" in body
    probe = subprocess.run(
        ["sh", "-c", 'sed "s|//[^/[:space:]]*@|//***@|g"'],
        input="https://npm.example/@scope/pkg\nhttps://u:p@ss@npm.example/@scope/pkg\n",
        capture_output=True,
        text=True,
        check=True,
        cwd=str(tmp_path),
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
    _native_tool_on_path("pwsh") is None and _native_tool_on_path("powershell") is None,
    reason="no native PowerShell binary on PATH to execute the installer's own helpers",
)
def test_the_powershell_helpers_behave_when_actually_executed(tmp_path: Path) -> None:
    """Everything else about the .ps1 in this suite is asserted from its TEXT,
    which is how four defects reached review -- text cannot tell you that a
    regex redacts what you meant or that a path API normalises. These two helpers
    are extracted from the shipped file and RUN, so the assertions are about
    behaviour. The script itself cannot be dot-sourced: it exits early off
    Windows, by design.
    """
    body = INSTALLER_PS1.read_text(encoding="utf-8")

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
        cwd=str(tmp_path),
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
    body = INSTALLER_PS1.read_text(encoding="utf-8")
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
    assert (tmp_path / "datahome" / "playwright-cli" / "managed-bin" / "playwright-cli").is_file()


def test_the_windows_installer_never_builds_a_cmd_command_line() -> None:
    """cmd.exe expands %VAR% while parsing a command line, so any path we embed in
    one can be rewritten before the target sees it -- and `%PATH%` is a legal
    directory name on NTFS. The install therefore runs node against npm's own
    npm-cli.js, with no shell at any point, and the wrapper is invoked through the
    call operator so PowerShell owns the quoting.
    """
    body = INSTALLER_PS1.read_text(encoding="utf-8")
    assert "cmd.exe /c" not in body, "a cmd.exe command line re-parses % in our paths"
    assert "npm-cli.js" in body, "the install must bypass npm.cmd when it can"
    assert "& $wrapper --version" in body


def test_the_windows_installer_stages_native_node_for_gateway_execution() -> None:
    body = INSTALLER_PS1.read_text(encoding="utf-8")
    assert '$gatewayNode = Join-Path $Prefix "node.exe"' in body
    assert "$nativeNode = Get-NodeProcessPath $script:NodeExe" in body
    assert "Copy-Item -LiteralPath $nativeNode" in body
    assert "Copy-Item -LiteralPath $script:NodeExe" not in body
    assert "Move-Item -LiteralPath $gatewayNodeTmp -Destination $gatewayNode -Force" in body
    assert "if (-not (Try-Node $gatewayNode))" in body
    assert "$stagedRuntime = Get-NodeProcessPath $gatewayNode" in body
    resolved = body.index("$nativeNode = Get-NodeProcessPath $script:NodeExe")
    copied = body.index("Copy-Item -LiteralPath $nativeNode")
    published = body.index(
        "Move-Item -LiteralPath $gatewayNodeTmp -Destination $gatewayNode -Force"
    )
    verified = body.index("if (-not (Try-Node $gatewayNode))")
    assert resolved < copied < published < verified


def test_the_posix_installer_stages_node_for_gateway_execution() -> None:
    body = INSTALLER_SH.read_text(encoding="utf-8")
    assert 'GATEWAY_NODE="$PREFIX/gateway-node"' in body
    assert "process.execPath" in body
    assert 'mktemp "$PREFIX/.gateway-node.XXXXXX"' in body
    assert 'mv "$_gateway_node_incoming" "$GATEWAY_NODE"' in body
    wrapper = body[body.index("# ── gateway Node + wrapper") : body.index("# ── verify")]
    assert 'exec $(_shell_quote "$GATEWAY_NODE") $(_shell_quote "$TARGET")' in wrapper
    assert 'PATH=$(_shell_quote "$NODE_BIN_DIR")' not in wrapper


def test_the_generated_wrapper_disables_delayed_expansion() -> None:
    """A path may legally contain `!`. Invoked from a shell started with
    `cmd /V:ON`, delayed expansion is inherited and would eat the `!` and its
    neighbours out of the PATH line, so the wrapper turns it off for its own
    scope. `%ERRORLEVEL%` is ordinary expansion and is unaffected.
    """
    body = INSTALLER_PS1.read_text(encoding="utf-8")
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

    sh = re.search(r"^MIN_NODE_MAJOR=(\d+)", INSTALLER_SH.read_text(encoding="utf-8"), re.MULTILINE)
    ps1 = re.search(
        r"^\$MinNodeMajor = (\d+)", INSTALLER_PS1.read_text(encoding="utf-8"), re.MULTILINE
    )
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
    ps1 = INSTALLER_PS1.read_text(encoding="utf-8")
    # Every ANCESTOR, not just the leaf: a junction one level up aliases the pair
    # while both leaves look ordinary and the strings differ.
    assert "function Find-ReparsePointAncestor" in ps1
    assert "Split-Path -Parent $current" in ps1, "the walk must climb to the root"
    # The shell installer needs no equivalent: `cd -P` resolves every component.
    assert 'cd -P -- "$PREFIX"' in INSTALLER_SH.read_text(encoding="utf-8")


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
    assert (tmp_path / "datahome" / "playwright-cli" / "managed-bin" / "playwright-cli").is_file()


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
    """The private Node tree remains the installer's repair toolchain, so a window
    in which that directory does not exist is a window in which an interrupted
    upgrade cannot retry cleanly. Deleting the old tree before promoting the new
    one creates exactly that window, and it is reachable on an ordinary path: a
    stamped, runnable Node with no npm is re-bootstrapped rather than reused. The
    old tree is moved aside and only removed once the new one is in place.
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
    sh = INSTALLER_SH.read_text(encoding="utf-8")
    aside = sh.index('mv "$PREFIX/node" "$_backup"')
    promote = sh.index('mv "$_stage/tree" "$PREFIX/node"')
    assert aside < promote, "the old tree must be moved aside before promotion"
    for match in re.finditer(r'rm -rf -- "\$PREFIX/node"', sh):
        assert match.start() > promote, (
            "removing the target before promotion is the window that breaks a " "working install"
        )

    ps1 = INSTALLER_PS1.read_text(encoding="utf-8")
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
            cwd=str(tmp_path),
        ).stdout.strip()
    )


def _signal_installer_group(pid: int, sig: int) -> None:
    """Signal the installer's whole process group, tolerating the macOS EPERM
    that ``killpg`` raises when it cannot signal every group member.

    The installer is spawned with ``start_new_session=True``, so ``pid`` is the
    group leader and ``killpg(pid, ...)`` targets the bootstrap's ``curl``/``tar``
    children too. On macOS, BSD ``kill(2)`` returns EPERM for a group signal when
    any member is in a state it cannot signal (e.g. a child mid-``exec`` or a
    reaped group leader) -- a documented divergence from Linux, where the same
    call succeeds. The intent here is only to interrupt the bootstrap, so on
    EPERM fall back to signaling the group leader (``sh``) directly: it forwards
    the signal or dies, and the EXIT trap still runs the restore the test
    asserts on. A group that has already exited is a no-op.
    """
    try:
        os.killpg(pid, sig)
    except PermissionError:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


@posix_only
def test_an_interrupted_rebootstrap_restores_the_previous_node(
    tmp_path: Path, stubs: Path, node_mirror: Path
) -> None:
    """SIGINT during promotion is the realistic interruption -- a user pressing
    Ctrl-C -- and it lands in the window where the old tree has been moved aside and
    the new one is not yet in place. The EXIT handler puts it back, so the wrapper
    never ends up pinned to a Node that is not there.

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
        cwd=str(tmp_path),
    )
    try:
        # Long enough to be inside the bootstrap, short enough to be mid-unpack.
        time.sleep(2.5)
        _signal_installer_group(proc.pid, signal.SIGINT)
        proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            _signal_installer_group(proc.pid, signal.SIGKILL)
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
    sh = INSTALLER_SH.read_text(encoding="utf-8")
    probe = sh.index('_try_node "$_stage/tree/bin/node"')
    aside = sh.index('mv "$PREFIX/node" "$_backup"')
    promote = sh.index('mv "$_stage/tree" "$PREFIX/node"')
    assert probe < aside < promote, "probe, then move aside, then promote"
    # The post-promotion probe is gone rather than merely reordered. Scoped to the
    # bootstrap: `_resolve_node` legitimately probes the installed location on a
    # LATER run, which is a different thing from probing a tree just promoted.
    bootstrap = sh[sh.index("_bootstrap_node() {") :]
    assert '_try_node "$PREFIX/node/bin/node"' not in bootstrap

    ps1 = INSTALLER_PS1.read_text(encoding="utf-8")
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
    body = INSTALLER_PS1.read_text(encoding="utf-8")
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
    body = INSTALLER_PS1.read_text(encoding="utf-8")
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
    body = INSTALLER_PS1.read_text(encoding="utf-8")
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
    body = INSTALLER_PS1.read_text(encoding="utf-8")
    staging = [ln for ln in body.splitlines() if "$staging = " in ln]
    assert staging, "staging assignment not found"
    assert "GetTempPath" not in staging[0]
    assert "$Prefix" in staging[0]


def test_the_bootstrapped_node_version_agrees_across_both_scripts() -> None:
    """Three copies of this version exist (both scripts and this suite's
    fixtures) with nothing tying them together, so a bump in one place would
    leave the tests green while not exercising the shipped value."""
    sh_version = re.search(
        r'^NODE_VERSION="([0-9.]+)"', INSTALLER_SH.read_text(encoding="utf-8"), re.MULTILINE
    )
    ps_version = re.search(
        r'^\s*\[string\]\$NodeVersion = "([0-9.]+)"',
        INSTALLER_PS1.read_text(encoding="utf-8"),
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
    body = INSTALLER_PS1.read_text(encoding="utf-8")
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
    body = INSTALLER_PS1.read_text(encoding="utf-8")
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
    ran = run_bounded(
        [str(wrapper), "--version"],
        env=_isolated_tool_env(tmp_path),
        timeout=120,
        cwd=str(tmp_path),
    )
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
    assert (tmp_path / "datahome" / "playwright-cli" / "managed-bin" / "playwright-cli").exists()


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
