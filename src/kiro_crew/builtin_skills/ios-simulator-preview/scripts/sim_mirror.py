#!/usr/bin/env python3
"""sim_mirror.py — boot an iOS Simulator and mirror it to a loopback URL.

Wraps `serve-sim` (npm) the way OpenAI Codex's ios-simulator-browser skill
does, adapted for KiroCrew:

  * launches serve-sim fully DETACHED (start_new_session) so it survives the
    agent turn ending (turn-end reaps normal child process groups),
  * pins the public npm registry (local npm defaults to an authenticated
    internal registry and 401s),
  * scoped cleanup only (per-UDID pidfiles + `serve-sim --kill <udid>`),
  * refuses non-loopback mirror URLs.

Commands:
  start  [--device <udid-or-name>]   boot (create if needed) + start mirror,
                                     prints JSON {udid, name, url, pid, log}
  stop   [--device <udid>] [--shutdown-device]
  status
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlparse


def die(msg: str) -> NoReturn:
    """Report a failure as the JSON object the calling agent parses, then exit.

    Defined ahead of the module constants because home resolution below runs at
    import time and must be able to fail this way.
    """
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(1)


# State lives under the ACTIVE data home, not a hardcoded ~/.kiro/crew, so a
# dev instance (KIROCREW_HOME=~/.kirocrew-dev) keeps its own pidfiles instead of
# sharing mirror state with a production install.
def _resolve_home() -> Path:
    """Resolve the data home to an absolute path, or refuse.

    A RELATIVE KIROCREW_HOME cannot be honored safely: this launcher is invoked
    by path from whatever directory the session happens to be in, so a relative
    home would resolve differently per caller — a `start` from one project
    directory and a `stop` from another would consult different state dirs and
    orphan the mirror. `~` is expanded (env vars routinely carry it unexpanded);
    anything still relative after that is a misconfiguration, so say so plainly
    instead of silently splitting state.
    """
    raw = os.environ.get("KIROCREW_HOME") or ""
    if not raw:
        return Path.home() / ".kiro" / "crew"
    home = Path(raw).expanduser()
    if not home.is_absolute():
        die(f"KIROCREW_HOME must be an absolute path, got {raw!r} "
            "(a relative home resolves differently per working directory, so "
            "start and stop would disagree about where mirror state lives)")
    return home


_HOME = _resolve_home()
STATE_DIR = _HOME / "workspace" / "sim-mirror"

# Pinned deliberately: floating the version (an `@latest` style specifier) would
# fetch and execute whatever npm currently serves on every single `start`, so a
# broken or hijacked release would silently change behavior (or run arbitrary
# code) with no diff. Bumping this is a reviewable one-line change.
SERVE_SIM_VERSION = "0.1.45"
NPX = [
    "npx", "--yes",
    "--registry=https://registry.npmjs.org",
    f"serve-sim@{SERVE_SIM_VERSION}",
]
URL_RE = re.compile(r"https?://[^\s\"'<>]+")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
# simctl UDIDs are canonical uppercase UUIDs. Anything else is rejected BEFORE
# it reaches a path join or a signal: `--device /tmp/svc` would otherwise make
# `STATE_DIR / f"{udid}.pid"` resolve to /tmp/svc.pid (an absolute right-hand
# operand replaces the base entirely), letting a caller read, unlink, and
# signal the process group named by an arbitrary file.
UDID_RE = re.compile(r"\A[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\Z")


def require_udid(value: str) -> str:
    """Return *value* if it is a canonical simctl UDID, else exit with an error."""
    if not UDID_RE.match(value):
        die(f"not a valid simulator UDID: {value!r} "
            "(pass the udid from `start`/`status`, or a device NAME to `start`)")
    return value


def sh(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """Run *args*, failing with a JSON diagnostic rather than a traceback.

    A wedged CoreSimulator makes simctl block until the deadline, and this tool's
    entire contract with the calling agent is one line of JSON on stdout or an
    error object on stderr. Letting TimeoutExpired escape would print a
    traceback the caller cannot parse, right at the moment it most needs to be
    told what to fix.
    """
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        die(f"timed out after {timeout}s: {' '.join(args)} "
            "(a wedged CoreSimulator is the usual cause -- see the skill's "
            "failure-mode notes on recovering an orphaned device)")
    except OSError as exc:  # missing binary, exec failure
        die(f"could not run {args[0]!r}: {exc}")


def sh_best_effort(args: list[str], timeout: int = 120) -> None:
    """Run *args* for its side effect only; never fail the caller.

    Used for cleanup paths where the tool has already decided what to report and
    a slow or absent helper must not turn a successful stop into an error.
    """
    try:
        subprocess.run(args, capture_output=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        pass


def simctl(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return sh(["xcrun", "simctl", *args], timeout=timeout)


def simctl_json(*args: str, timeout: int = 120) -> dict:
    """Run a `simctl -j` query and return its parsed object, or fail loudly.

    simctl can exit nonzero (or print a non-JSON diagnostic on stdout) when the
    CoreSimulator service is wedged or a required platform is absent. Feeding
    that straight to json.loads raised JSONDecodeError, so a recoverable
    environment problem reached the caller as a traceback with the real reason --
    simctl's own stderr -- discarded.
    """
    p = simctl(*args, timeout=timeout)
    if p.returncode != 0:
        detail = (p.stderr or p.stdout or "").strip().splitlines()
        die(f"simctl {' '.join(args)} failed (rc={p.returncode}): "
            f"{detail[0] if detail else 'no output'}")
    try:
        parsed = json.loads(p.stdout)
    except json.JSONDecodeError:
        head = (p.stdout or "").strip()[:200]
        die(f"simctl {' '.join(args)} did not return JSON: {head!r}")
    if not isinstance(parsed, dict):
        die(f"simctl {' '.join(args)} returned {type(parsed).__name__}, expected an object")
    return parsed


def list_devices() -> list[dict]:
    devices = simctl_json("list", "-j", "devices", "available").get("devices", {})
    flat: list[dict] = []
    for runtime_id, devs in devices.items():
        if "iOS" not in runtime_id:
            continue
        for d in devs:
            d["runtime"] = runtime_id
            flat.append(d)
    return flat


def create_device() -> dict:
    """No iOS devices exist — create one the newest installed runtime supports."""
    runtimes = [
        r
        for r in simctl_json("list", "-j", "runtimes").get("runtimes", [])
        if r.get("platform") == "iOS" and r.get("isAvailable")
    ]
    if not runtimes:
        die("no available iOS runtimes (run: xcodebuild -downloadPlatform iOS)")
    runtime = runtimes[-1]
    # Pick from the runtime's OWN supported types — the global devicetypes list
    # includes newer hardware than an older runtime accepts ("Incompatible device").
    supported = runtime.get("supportedDeviceTypes") or []
    phones = [t for t in supported if t.get("productFamily") == "iPhone"]
    if not phones:
        types = simctl_json("list", "-j", "devicetypes").get("devicetypes", [])
        phones = [t for t in types if str(t.get("name", "")).startswith("iPhone")]
    if not phones:
        die("no iPhone device types installed (install the iOS platform in Xcode)")
    dtype = phones[-1]  # lists run oldest→newest
    name = f"KiroCrew {dtype['name']}"
    out = simctl("create", name, dtype["identifier"], runtime["identifier"])
    if out.returncode != 0:
        die(f"simctl create failed: {out.stderr.strip()}")
    return {"udid": out.stdout.strip(), "name": name, "state": "Shutdown"}


def resolve_device(want: str | None) -> dict:
    devs = list_devices()
    if want:
        for d in devs:
            if d["udid"] == want or d["name"] == want:
                return d
        die(f"device not found: {want}")
    booted = [d for d in devs if d["state"] == "Booted"]
    if booted:
        return booted[0]
    phones = [d for d in devs if d["name"].startswith(("iPhone", "KiroCrew"))]
    if phones:
        return phones[-1]
    if devs:
        return devs[-1]
    return create_device()


def ensure_booted(udid: str) -> None:
    out = simctl("boot", udid)
    # rc 149 / "current state: Booted" == already booted; anything else fatal.
    if out.returncode != 0 and "Booted" not in (out.stderr + out.stdout):
        die(f"simctl boot failed: {out.stderr.strip()}")
    # Block until userspace is fully up (fast no-op when already booted).
    simctl("bootstatus", udid, "-b", timeout=300)


def mirror_url_from_log(log: Path) -> tuple[str | None, str | None]:
    """(loopback_url, warning). Non-loopback hits produce a warning, never a URL."""
    text = log.read_text(errors="ignore") if log.exists() else ""
    warning = None
    for m in URL_RE.finditer(text):
        host = (urlparse(m.group(0)).hostname or "").lower()
        if host in LOOPBACK_HOSTS:
            return m.group(0).rstrip(".,"), None
        if host and "registry" not in m.group(0):
            warning = f"serve-sim printed non-loopback URL {m.group(0)} — not using it"
    return None, warning


def cmd_start(args: argparse.Namespace) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    dev = resolve_device(args.device)
    udid, name = dev["udid"], dev["name"]
    ensure_booted(udid)

    # Stale-mirror cleanup, reached only for a LIVE mirror THIS home owns for
    # this udid. The vendor flag addresses a mirror by UDID alone and cannot tell
    # whose it is, so a weaker gate (pidfile merely present) would let a `start`
    # here tear down a live mirror another KIROCREW_HOME owns for the device.
    if _live_owned_pid(udid) is not None:
        sh_best_effort(NPX + ["--kill", udid])
    pidfile = STATE_DIR / f"{udid}.pid"
    if pidfile.exists():
        pidfile.unlink(missing_ok=True)

    log = STATE_DIR / f"{udid}.log"
    try:
        # O_NOFOLLOW: the log path is predictable, so a symlink planted there
        # would otherwise be followed and its target truncated by the "w" open.
        # Refuse rather than write through one. 0o600 keeps the log private.
        log_fd = os.open(
            log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
    except OSError as exc:
        die(f"refusing to open the mirror log at {log}: {exc} "
            "(a symlink at that path is not written through -- remove it and retry)")
    try:
        with open(log_fd, "w", closefd=True) as lf:
            proc = subprocess.Popen(
                NPX + [udid],
                stdout=lf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # survives agent turn-end process-group reap
            )
    except OSError as exc:
        # Popen bypasses sh()'s conversion, so a missing npx (Node not installed)
        # would surface as a traceback instead of the JSON error the caller parses.
        die(f"could not start the mirror -- {NPX[0]!r} is not runnable ({exc}). "
            "Install Node (which provides npx), then retry.")
    pidfile.write_text(str(proc.pid))

    deadline = time.time() + 240  # first run downloads the package
    url = warning = None
    while time.time() < deadline:
        if proc.poll() is not None:
            die(f"serve-sim exited rc={proc.returncode}; see log: {log}")
        url, warning = mirror_url_from_log(log)
        if url:
            break
        time.sleep(1)
    if not url:
        die(f"no loopback URL from serve-sim within 240s"
            f"{'; ' + warning if warning else ''}; see log: {log}")

    print(json.dumps({"udid": udid, "name": name, "url": url,
                      "pid": proc.pid, "log": str(log)}))


def _owns_pid(pid: int, udid: str) -> bool:
    """True only if *pid* is still OUR mirror for *udid*.

    A recorded pid is not proof of ownership: once the mirror exits the OS is
    free to reuse that number, and signaling it blind would terminate whatever
    unrelated process group now holds it. So re-derive identity from the live
    process's own command line — it must be the serve-sim invocation carrying
    this udid — and treat any doubt as "not ours".
    """
    try:
        p = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if p.returncode != 0:
        return False  # pid is gone
    cmd = p.stdout.strip()
    return "serve-sim" in cmd and udid in cmd


def _live_owned_pid(udid: str) -> int | None:
    """Return the pid of THIS home's live mirror for *udid*, else None.

    Ownership means a process of ours is running right now — not merely that a
    pidfile exists. A stale pidfile (our mirror already exited) is NOT ownership:
    acting on it would let this home's cleanup reach a live mirror another
    KIROCREW_HOME legitimately owns for the same device, since the vendor's
    cleanup flag addresses a mirror by UDID alone and cannot tell whose it is.
    """
    pidfile = STATE_DIR / f"{udid}.pid"
    if not pidfile.exists():
        return None
    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError):
        return None
    if pid <= 0 or not _owns_pid(pid, udid):
        return None
    return pid


def stop_one(udid: str, shutdown_device: bool) -> dict:
    pidfile = STATE_DIR / f"{udid}.pid"
    tracked = pidfile.exists()
    pid = _live_owned_pid(udid)
    killed = False
    if pid is not None:
        try:
            os.killpg(pid, signal.SIGTERM)
            killed = True
        except (ProcessLookupError, PermissionError):
            pass
        # Vendor-side cleanup, reached only once a LIVE mirror of ours is proven.
        # Gating on the pidfile merely existing was not enough: a stale pidfile
        # here would have authorized tearing down another home's live mirror for
        # the same device. If our process is gone there is nothing of ours left
        # to clean up, so the correct action is to drop the pidfile and stop.
        sh_best_effort(NPX + ["--kill", udid])
    if tracked:
        pidfile.unlink(missing_ok=True)
    if shutdown_device:
        # Explicitly requested by the caller and scoped to this one device, so it
        # is honored even for a mirror this home never tracked.
        simctl("shutdown", udid)
    return {"udid": udid, "tracked": tracked, "killed": killed,
            "device_shutdown": shutdown_device}


def cmd_stop(args: argparse.Namespace) -> None:
    udids = ([require_udid(args.device)] if args.device
             else [p.stem for p in STATE_DIR.glob("*.pid") if UDID_RE.match(p.stem)])
    if not udids:
        print(json.dumps({"stopped": []}))
        return
    print(json.dumps({"stopped": [stop_one(u, args.shutdown_device) for u in udids]}))


def cmd_status(_args: argparse.Namespace) -> None:
    entries = []
    for p in sorted(STATE_DIR.glob("*.pid")):
        if not UDID_RE.match(p.stem):
            continue  # not a pidfile this tool wrote
        try:
            pid = int(p.read_text().strip())
        except (ValueError, OSError):
            # ValueError: truncated/garbled pidfile — nothing trustworthy to
            # report. OSError: a concurrent `stop` (or another session) unlinked
            # the file between the glob above and this read; status is a
            # read-only snapshot and must not crash on a benign race.
            continue
        url, _ = mirror_url_from_log(STATE_DIR / f"{p.stem}.log")
        entries.append({
            "udid": p.stem,
            "pid": pid,
            # "alive" means OUR mirror is still running, not merely that some
            # process holds this pid (see _owns_pid on pid reuse).
            "alive": _owns_pid(pid, p.stem),
            "url": url,
        })
    print(json.dumps({"mirrors": entries}))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_start = sub.add_parser("start")
    p_start.add_argument("--device", help="simulator UDID or name")
    p_start.set_defaults(fn=cmd_start)
    p_stop = sub.add_parser("stop")
    p_stop.add_argument("--device", help="simulator UDID (default: all tracked)")
    p_stop.add_argument("--shutdown-device", action="store_true")
    p_stop.set_defaults(fn=cmd_stop)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
