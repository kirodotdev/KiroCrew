"""Every URL the Playwright CLI installers print is redacted, in every message.

No user-facing message interpolates a raw URL variable, the dry-run plan and the
validation error redact what they print, and a log that cannot be scrubbed is
discarded. The same section also holds the guards for a bin dir that would replace
the package's own, the Windows native calls and wrapper target, PowerShell function
order, an empty Node marker, and the isolated npm environment.
Shared stubs, environments and constants live in
``playwright_cli_installer_helpers.py``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from installer_test_helpers import run_bounded
from playwright_cli_installer_helpers import (  # noqa: F401 -- fixtures are used by name
    EXIT_CODES,
    INSTALLER_PS1,
    INSTALLER_SH,
    _fake_node,
    _fake_npm_succeeding,
    _is_native_executable,
    _isolated_tool_env,
    _run,
    _write_stub,
    posix_only,
    stubs_fixture,
)


def _real_npm() -> tuple[Path, Path] | None:
    """``(node, npm-cli.js)`` for the first npm on PATH that is npm itself, else None.

    npm is a node script: its ``npm`` entry is a symlink to ``npm-cli.js`` whose
    shebang is ``#!/usr/bin/env node``, so running it by name would resolve
    ``node`` through PATH -- and hit the shim again. The pair is therefore
    resolved statically (the ``npm`` entry must resolve to ``npm-cli.js`` and the
    ``node`` beside it must be a native binary) and invoked as
    ``node npm-cli.js ...``, which bypasses every shim and needs nothing from HOME.
    Same verdict on every run of one host, by construction.
    """
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        try:
            npm_cli = (Path(entry) / "npm").resolve(strict=True)
            node = (Path(entry) / "node").resolve(strict=True)
        except OSError:
            continue
        if npm_cli.name == "npm-cli.js" and node.name == "node" and _is_native_executable(node):
            return node, npm_cli
    return None


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

    for line in INSTALLER_SH.read_text(encoding="utf-8").splitlines():
        stripped = _message_part(line.strip(), emitters_sh)
        if not stripped or line.strip().startswith("#"):
            continue
        for var in RAW_URL_VARS_SH:
            # The negative lookahead is what lets the DISPLAY companion through:
            # $NODE_ART_MIRROR_DISPLAY must not read as a hit on $NODE_ART_MIRROR.
            if re.search(re.escape(var) + r"(?![A-Za-z0-9_])", stripped):
                offenders.append(f"sh: {stripped}")
    emitters_ps = ("Write-Host ", "Say ", "Warn ", "Die ")
    for line in INSTALLER_PS1.read_text(encoding="utf-8").splitlines():
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
    body = INSTALLER_SH.read_text(encoding="utf-8")
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
    body = INSTALLER_PS1.read_text(encoding="utf-8")
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
    body = INSTALLER_PS1.read_text(encoding="utf-8")
    assert "GetFullPath($wrapper) -eq [System.IO.Path]::GetFullPath($target)" in body


def test_every_powershell_function_is_defined_before_it_is_called() -> None:
    """PowerShell resolves functions at CALL time, walking the script top-down, so
    a helper defined below its first use is a runtime crash rather than a parse
    error — and it replaces the honest usage error with an opaque exit 1.

    This is the class behind moving the redaction helper above the https
    validator: the shell twin was reordered and this one was not, so a
    lockstep check is the only thing that catches the asymmetry.
    """
    lines = INSTALLER_PS1.read_text(encoding="utf-8").splitlines()
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
    body = INSTALLER_PS1.read_text(encoding="utf-8")
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
    assert (tmp_path / "datahome" / "playwright-cli" / "managed-bin" / "playwright-cli").exists()


@posix_only
@pytest.mark.skipif(
    _real_npm() is None,
    reason="no real npm on PATH to validate the config files with (a version-manager "
    "shim does not count: it cannot start under the isolated HOME this suite requires)",
)
def test_real_npm_accepts_the_isolated_config_files(tmp_path: Path, stubs: Path) -> None:
    """--isolated-npmrc is THE documented remedy for the enterprise auth failure,
    so it has to work against real npm, not just against a stub. npm rejects one
    path used as two scopes, so the two empty files must be distinct — and only
    real npm can confirm that. `config get` needs no network.

    The npm is resolved by ``_real_npm`` -- npm-cli.js plus the native node beside
    it -- and invoked as ``node npm-cli.js``, so no version-manager shim is ever in
    the process tree. That is what lets this test hold the same verdict on every
    run: the earlier version ran whatever ``npm`` PATH resolved to, and on a
    developer machine that is a shim which, under the isolated HOME, blocks trying
    to provision a toolchain -- so the test skipped or passed by the clock, and each
    skip cost a SIGKILL of the shim's tree. A real npm either answers or is a
    failure; nothing here is decided by a timeout.
    """
    _fake_node(stubs)
    _fake_npm_succeeding(stubs)
    result = _run(tmp_path, stubs, "--version", "0.1.18", "--isolated-npmrc")
    assert result.returncode == 0, result.stdout + result.stderr

    isolated = tmp_path / "datahome" / "playwright-cli" / "isolated-npmrc"
    user, global_ = isolated / "user", isolated / "global"
    assert user.is_file() and global_.is_file()
    assert user != global_

    # HOME stays isolated: npm's own write targets must resolve inside tmp_path,
    # which is the side effect this suite exists to prevent leaking.
    env = _isolated_tool_env(
        tmp_path,
        npm_config_userconfig=str(user),
        npm_config_globalconfig=str(global_),
    )
    node, npm_cli = _real_npm()  # type: ignore[misc]  # the skipif above proved it
    # Bounded through `run_bounded` so a wedged npm cannot leave a tree behind; a
    # timeout propagates as the error it is rather than becoming a skip.
    probe = run_bounded(
        [str(node), str(npm_cli), "config", "get", "registry", "cache", "logs-dir"],
        env=env,
        timeout=60,
        cwd=str(tmp_path),
    )
    combined = probe.stdout + probe.stderr
    # The failure this test exists to catch: npm refusing the two config files.
    assert "double-loading config" not in combined, combined
    assert probe.returncode == 0, combined
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
