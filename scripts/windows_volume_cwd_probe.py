#!/usr/bin/env python3
r"""Real-Windows probe: do the agent harnesses start under a ``\\?\Volume{GUID}\`` cwd?

WHY THIS EXISTS. A project-scoped session on Windows is created with the
volume-identity spelling of its work dir as the process's current directory
(``AcpRuntime._pin_work_dir_chain`` / ``AcpClient._pin_work_dir_chain``): the
drive letter the validated spelling starts from is a name the mount manager can
rebind, and only a spelling the kernel resolves through the volume carries the
chain pin's verdict to ``CreateProcess``'s own open of the cwd. That is sound
only if the harnesses the gateway spawns actually START under such a cwd and
see the right directory there -- a harness that string-handles its cwd and
chokes on the ``\\?\`` prefix would fail every project-scoped Windows session
at spawn. No unit test can answer that: it is a property of kiro-cli's and
node's own runtimes on a real Windows kernel. This script is run by the
``windows-volume-cwd-probe`` job in ``.github/workflows/ci.yml`` on a hosted
Windows runner and fails the job on the first harness that does not.

WHAT IT PROVES, per child created with ``cwd=<volume-identity spelling>``:

* ``python`` reports a cwd that IS the validated directory (``samefile``);
* ``node`` (the claude-agent-acp adapter's runtime) reports a cwd that IS the
  validated directory, lists its entries, and ``path.resolve`` works under it;
* ``kiro-cli.exe --version`` exits 0 under it;
* ``kiro-cli.exe acp`` answers the ACP ``initialize`` handshake under it -- the
  same first request the gateway sends -- so the harness is up and reading
  stdin, not merely printing a banner.

And, with the chain pinned, that the validated directory cannot be renamed
(the hold the pin exists for, and the user-visible consequence the spec names).

WHAT IT DOES NOT PROVE: that a signed-in kiro-cli loads ``.kiro/steering`` from
that cwd -- the runner has no kiro-cli login, so no ``session/new`` +
``session/prompt`` runs here. The steering load is a by-path read under a cwd
the process has already accepted; the part this probe pins is the acceptance.

The kiro-cli binary comes from the SAME pin the desktop installer bundles
(``packaging/kiro-cli-version`` + ``packaging/kiro-cli-sha256``), fetched and
sha256-verified here, then laid out by an administrative ``msiexec /a`` extract
(no install, no registry, no PATH) -- the procedure ``packaging/build-desktop.sh``
uses. Pass ``--kiro-cli <path>`` to probe an already-present binary instead.

Exit status: 0 when every probe passes, 1 on any failure (with the reason on
stderr and in ``$GITHUB_STEP_SUMMARY`` when set), 2 off Windows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_RELEASE_BASE = "https://prod.download.cli.kiro.dev/stable"
WINDOWS_MSI = "kiro-cli-x86_64-pc-windows-msvc.msi"
ACP_HANDSHAKE_TIMEOUT_S = 90.0


def _summary(lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    sys.stdout.write(text)
    sys.stdout.flush()
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("### Windows volume-identity cwd probe\n\n")
            fh.write("```\n" + text + "```\n")


def _fail(reason: str, lines: list[str]) -> int:
    lines.append(f"FAIL: {reason}")
    _summary(lines)
    sys.stderr.write(f"::error::{reason}\n")
    return 1


def _pinned_sha(version: str, file: str) -> str:
    want = f"{version}/{file}"
    for raw in (ROOT / "packaging" / "kiro-cli-sha256").read_text(encoding="utf-8").splitlines():
        parts = raw.split(None, 1)
        if len(parts) == 2 and parts[1].strip() == want:
            return parts[0]
    raise SystemExit(f"packaging/kiro-cli-sha256 names no sha256 for {want!r}")


def fetch_kiro_cli(into: Path) -> Path:
    """Download the pinned Windows MSI, verify it, extract it; return kiro-cli.exe."""
    version = (ROOT / "packaging" / "kiro-cli-version").read_text(encoding="utf-8").strip()
    sha = _pinned_sha(version, WINDOWS_MSI)
    url = f"{WINDOWS_RELEASE_BASE}/{version}/{urllib.parse.quote(WINDOWS_MSI)}"
    msi = into / WINDOWS_MSI
    print(f"fetching kiro-cli {version}: {url}")
    with urllib.request.urlopen(url, timeout=120) as resp, open(msi, "wb") as out:  # noqa: S310
        shutil.copyfileobj(resp, out)
    digest = hashlib.sha256(msi.read_bytes()).hexdigest()
    if digest != sha:
        raise SystemExit(f"kiro-cli MSI sha256 mismatch: got {digest}, pinned {sha}")
    target = into / "extract"
    target.mkdir()
    # Administrative extraction: lays the payload out without installing it.
    subprocess.run(
        ["msiexec.exe", "/a", str(msi), "/qn", f"TARGETDIR={target}"],
        check=True,
        timeout=300,
    )
    found = [p for p in target.rglob("kiro-cli.exe")]
    if len(found) != 1:
        raise SystemExit(f"expected exactly one kiro-cli.exe in the MSI payload, found {found}")
    return found[0]


def _run(argv: list[str], cwd: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def acp_initialize(kiro_cli: Path, cwd: str) -> tuple[bool, str]:
    """Start ``kiro-cli acp`` under *cwd*, send ``initialize``, wait for its reply."""
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": 1,
            "clientInfo": {"name": "kirocrew-windows-volume-cwd-probe", "version": "0"},
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
        },
    }
    proc = subprocess.Popen(
        [str(kiro_cli), "acp"],
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    try:
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + ACP_HANDSHAKE_TIMEOUT_S
        seen: list[str] = []
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                err = proc.stderr.read()
                return False, (
                    f"kiro-cli acp exited {proc.returncode} before answering initialize; "
                    f"stdout={seen!r} stderr={err[-2000:]!r}"
                )
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            seen.append(line.rstrip())
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == 1:
                if "result" in msg:
                    return True, f"initialize answered: {json.dumps(msg['result'])[:400]}"
                return False, f"initialize returned an error: {json.dumps(msg)[:800]}"
        return False, f"no initialize reply within {ACP_HANDSHAKE_TIMEOUT_S:.0f}s; stdout={seen!r}"
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--kiro-cli",
        default="fetch",
        help="path to kiro-cli.exe, or 'fetch' (default) to download the pinned release",
    )
    parser.add_argument("--node", default=None, help="path to node.exe (default: from PATH)")
    args = parser.parse_args(argv)

    if os.name != "nt":
        print("this probe is Windows-only (the volume identity is a Windows kernel name)")
        return 2

    sys.path.insert(0, str(ROOT / "src"))
    from kiro_crew import platform_compat

    lines: list[str] = []
    scratch = Path(tempfile.mkdtemp(prefix="kc-volcwd-", dir=os.environ.get("RUNNER_TEMP")))
    try:
        repo = scratch / "project" / "repo"
        (repo / ".kiro" / "steering").mkdir(parents=True)
        (repo / ".kiro" / "steering" / "probe.md").write_text("# probe\n", encoding="utf-8")
        (repo / "marker.txt").write_text("marker\n", encoding="utf-8")

        node = args.node or shutil.which("node")
        if not node:
            return _fail("node not found on PATH (the claude-agent-acp adapter's runtime)", lines)
        kiro_cli = (
            fetch_kiro_cli(scratch / "kiro-cli")
            if args.kiro_cli == "fetch"
            else Path(args.kiro_cli)
        )
        if not kiro_cli.is_file():
            return _fail(f"kiro-cli not found at {kiro_cli}", lines)

        # The PR's OWN pin, exactly as the spawn owners call it.
        fds, bound = platform_compat.pin_directory_chain_bound(str(repo), create_missing=True)
        try:
            lines.append(f"validated spelling : {repo}")
            lines.append(f"bound spelling     : {bound}")
            if not bound.startswith("\\\\?\\Volume{"):
                return _fail("the pin did not bind to a volume identity on this runner", lines)

            # 1. python under the identity cwd.
            r = _run([sys.executable, "-c", "import os; print(os.getcwd())"], cwd=bound)
            if r.returncode != 0 or not os.path.samefile(r.stdout.strip(), repo):
                return _fail(
                    f"python under identity cwd: rc={r.returncode} out={r.stdout!r} err={r.stderr!r}",
                    lines,
                )
            lines.append(f"python  cwd        : {r.stdout.strip()}  (samefile: ok)")

            # 2. node under the identity cwd: cwd, directory listing, path.resolve.
            script = (
                "const fs=require('fs');const path=require('path');"
                "console.log(process.cwd());"
                "console.log(fs.readdirSync('.').sort().join(','));"
                "console.log(path.resolve('.kiro','steering','probe.md'));"
                "console.log(fs.readFileSync('marker.txt','utf8').trim());"
            )
            r = _run([node, "-e", script], cwd=bound)
            out = r.stdout.splitlines()
            if r.returncode != 0 or len(out) < 4:
                return _fail(
                    f"node under identity cwd: rc={r.returncode} out={r.stdout!r} err={r.stderr!r}",
                    lines,
                )
            if not os.path.samefile(out[0].strip(), repo):
                return _fail(f"node cwd {out[0]!r} is not the validated directory", lines)
            if out[1].strip() != ".kiro,marker.txt" or out[3].strip() != "marker":
                return _fail(f"node did not read the validated directory's entries: {out!r}", lines)
            if not os.path.isfile(out[2].strip()):
                return _fail(
                    f"node path.resolve under identity cwd names nothing: {out[2]!r}", lines
                )
            lines.append(f"node    cwd        : {out[0].strip()}  (samefile, readdir, resolve: ok)")

            # 3. kiro-cli --version under the identity cwd.
            r = _run([str(kiro_cli), "--version"], cwd=bound)
            if r.returncode != 0:
                return _fail(
                    f"kiro-cli --version under identity cwd: rc={r.returncode} err={r.stderr!r}",
                    lines,
                )
            lines.append(f"kiro-cli --version : {r.stdout.strip() or r.stderr.strip()}  (rc 0)")

            # 4. kiro-cli acp initialize handshake under the identity cwd.
            ok, detail = acp_initialize(kiro_cli, bound)
            if not ok:
                return _fail(f"kiro-cli acp under identity cwd: {detail}", lines)
            lines.append(f"kiro-cli acp       : {detail}")

            # 5. The hold: the validated directory cannot be renamed while pinned.
            try:
                os.rename(repo, repo.with_name("repo.moved"))
            except PermissionError as exc:
                lines.append(f"rename while pinned: refused ({exc.strerror})  (ok)")
            else:
                return _fail(
                    "the pinned directory could be renamed -- the chain holds nothing", lines
                )
        finally:
            platform_compat.release_directory_chain(fds)

        lines.append("PASS: every harness starts under the volume-identity cwd")
        _summary(lines)
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
