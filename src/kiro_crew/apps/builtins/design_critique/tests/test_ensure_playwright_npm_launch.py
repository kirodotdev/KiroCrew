"""Tests: ensure-playwright.mjs can launch npm on Windows.

The Windows Node installer ships npm only as ``npm.cmd`` beside ``node.exe``.
A shell-less ``spawnSync('npm', ...)`` cannot find it (``ENOENT``), which
leaves the one-time Playwright install unrun and every capture exiting 3. On
Windows the script runs ``node_modules/npm/bin/npm-cli.js`` through the running
Node when it sits beside the binary, and a failed launch names its error code.

All cases share ONE ``node`` child, on one worker, for the same cold-start
reason as ``test_ssrf_guard_fail_closed.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kiro_crew.testing.wait import default_timeout

pytestmark = pytest.mark.xdist_group(name="node_probe_ensure_playwright_npm_launch")

APP_ROOT = Path(__file__).resolve().parents[1]
ENSURE_PW = APP_ROOT / "skills/design-critique/scripts/ensure-playwright.mjs"

# The launch-failure case empties PATH so `npm` by name cannot be found. It runs
# only off Windows: there the real npm-cli.js beside node would start a real
# install, which needs the network.
_PROBE = """
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
const mod = await import(%(script)s)
const { getPlaywright } = mod
// A script without the helper yields null verdicts, so the tests fail by name.
const npmLaunch = mod.npmLaunch || (() => null)
const withCli = %(with_cli)s, without = %(without)s
const cli = join(withCli, 'node_modules', 'npm', 'bin', 'npm-cli.js')
mkdirSync(join(withCli, 'node_modules', 'npm', 'bin'), { recursive: true })
writeFileSync(cli, '')
const args = ['install', '--prefix', 'C:\\\\a b&c', 'playwright@1.58.2']
const out = {
  win_with_cli: npmLaunch(args, { execPath: join(withCli, 'node.exe'), platform: 'win32' }),
  win_without_cli: npmLaunch(args, { execPath: join(without, 'node.exe'), platform: 'win32' }),
  posix_with_cli: npmLaunch(args, { execPath: join(withCli, 'node'), platform: 'linux' }),
  cli, args,
}
if (process.platform !== 'win32') {
  process.env.PATH = ''
  const lines = []
  const orig = console.error
  console.error = (m) => lines.push(String(m))
  try { out.failed_result = await getPlaywright() } finally { console.error = orig }
  out.failed_lines = lines
}
console.log(JSON.stringify(out))
"""


@pytest.fixture(scope="module")
def verdicts(tmp_path_factory: pytest.TempPathFactory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    root = tmp_path_factory.mktemp("npm_launch_probe")
    # A copy outside the checkout, so no real playwright above it is resolved.
    script = root / "script" / ENSURE_PW.name
    script.parent.mkdir()
    shutil.copy2(ENSURE_PW, script)
    (root / "cache").mkdir()
    probe = _PROBE % {
        "script": json.dumps(script.as_uri()),
        "with_cli": json.dumps(str(root / "with_cli")),
        "without": json.dumps(str(root / "without")),
    }
    proc = subprocess.run(
        [node, "--input-type=module", "-e", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
        cwd=root,
        env={**os.environ, "DC_PW_DIR": str(root / "cache")},
    )
    assert proc.returncode == 0, f"node exited {proc.returncode}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_windows_runs_npm_cli_through_node(verdicts):
    cmd, args = verdicts["win_with_cli"]
    assert cmd.endswith("node.exe")
    assert args == [verdicts["cli"], *verdicts["args"]]


def test_windows_without_npm_cli_keeps_npm_by_name(verdicts):
    assert verdicts["win_without_cli"] == ["npm", verdicts["args"]]


def test_posix_launch_unchanged(verdicts):
    assert verdicts["posix_with_cli"] == ["npm", verdicts["args"]]


def test_failed_launch_names_error_code(verdicts):
    if "failed_lines" not in verdicts:
        pytest.skip("launch-failure case runs off Windows only")
    assert verdicts["failed_result"] is None
    failed = [line for line in verdicts["failed_lines"] if "install failed" in line]
    assert failed and "ENOENT" in failed[0], verdicts["failed_lines"]
