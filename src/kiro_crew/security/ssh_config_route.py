"""Prove three ssh config routes for a dotted target with no DNS record.

Only a HostName rewrite without a proxy, one ProxyJump without ProxyCommand,
or exactly ``ssh [-q] -W %h:%p host`` (either option order) qualifies. Every
hop address must pass the live IP-host guard. Host patterns treat only ``*``
and ``?`` as wildcards, as in OpenSSH. A matching Host or Match block
that sets a destination key, other proxies, extra flags, hop chains and
nonliteral hop destinations refuse. Proxy forms require a literal final HostName
and check its IP literal or locally resolved addresses, without checking its Host
block; a literal name with no local DNS answer is left for the proxy to resolve.
A route-cleared name has the same standing as a DNS-cleared name: outer
``-F``/``-S``/``rsync -e`` routing remains the documented network-fence residual.
Config bytes and Includes are sandbox-read into a snapshot fed to ssh -G on stdin.
NUL, lone CR and whitespace other than space, tab and newline refuse before
parsing; CRLF line endings are accepted.
The proof runs ssh, sh and head from trusted system directories, never through PATH.
Unreadable files, nested-block Includes, unsafe Match criteria and absent
sandboxing or trusted executables refuse. Runs off-loop; failures cannot retain
a route-only allow.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import os
import pathlib
import socket
import subprocess
from dataclasses import dataclass

from kiro_crew import platform_compat
from kiro_crew.platform_compat import IS_WINDOWS
from kiro_crew.sandbox_launcher_program import _ssh_config_words

# How long ``ssh -G`` and each sandboxed config read may take, and how many
# nested Include levels are read (OpenSSH's own nesting limit is 16).
_SSH_G_TIMEOUT_SECS = 5.0
_INCLUDE_DEPTH_MAX = 16
# A config file larger than this is not read, so the route is not proven; the
# snapshot of every file read together has its own cap.
_CONFIG_BYTES_MAX = 1 << 20
_SNAPSHOT_BYTES_MAX = 4 << 20
# Resets ssh to the top-level state, as the end of an included file does.
_MATCH_ALL = "Match all"
_ROUTE_KEYS = ("hostname", "proxyjump", "proxycommand")
# Keys that can change where a section's host connects: a section that sets
# none of them (SendEnv, User, Port, ...) leaves the destination alone.
_DESTINATION_KEYS = _ROUTE_KEYS + (
    "canonicalizehostname",
    "canonicaldomains",
    "canonicalizefallbacklocal",
    "canonicalizemaxdots",
    "canonicalizepermittedcnames",
)

# The two programs run inside the agent's sandbox to read config.  Each takes
# its path or glob as a positional argument, never inside the script text.
# Prints the first $2 bytes of $1 with the trusted head at $3; exits 97 if absent.
_READ_SCRIPT = 'if [ -e "$1" ]; then exec "$3" -c "$2" -- "$1"; fi; exit 97'
_READ_MISSING = 97
# Prints every existing path the glob $1 expands to, each ending in NUL.  IFS
# is empty, so the unquoted $1 is expanded as a glob but never split.
_GLOB_SCRIPT = 'IFS=; for f in $1; do if [ -e "$f" ]; then printf "%s\\0" "$f"; fi; done'
_GLOB_CHARS = "*?["


class _Unknowable(Exception):
    """The config cannot be read in full, so nothing is proven."""


@dataclass
class _Block:
    """One ``Host`` or ``Match`` section, or the lines before any of them."""

    kind: str  # "all", "host" or "match"
    patterns: "list[str]"
    routes: bool = False  # it sets one of _DESTINATION_KEYS


def _config_roots() -> "list[tuple[str, str]]":
    """The files ``ssh -G`` reads by default, each with its Include base directory."""
    ssh_dir = os.path.join(os.path.expanduser("~"), ".ssh")
    if IS_WINDOWS:
        system_dir = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "ssh")
    else:
        system_dir = str(pathlib.Path(os.sep, "etc", "ssh"))
    return [
        (os.path.join(ssh_dir, "config"), ssh_dir),
        (os.path.join(system_dir, "ssh_config"), system_dir),
    ]


def _bare(line: str) -> str:
    """*line* lowercased with every quote and backslash removed, as ssh reads a word."""
    return line.replace('"', "").replace("'", "").replace("\\", "").lower()


def _keyword(line: str) -> str:
    """The keyword ssh reads from *line*, even when it is quoted or escaped."""
    words = _ssh_config_words(_bare(line))
    return words[0] if words else ""


def _is_match_exec(line: str) -> bool:
    """True for a ``Match`` line that could hold an ``exec`` criterion in any spelling."""
    return _keyword(line) == "match" and "exec" in _bare(line)


def _has_match_exec(text: str) -> bool:
    return any(_is_match_exec(line.strip()) for line in text.splitlines())


def _sandbox_mode() -> "str | None":
    """The agent's sandbox tier after the governance clamp, or None when it confines nothing."""
    from kiro_crew import sandbox_tiers, sandbox_wrap

    mode = sandbox_tiers.effective_sandbox_mode(sandbox_tiers.configured_sandbox_mode())
    if mode == "off" or sandbox_wrap.detect_backend(mode) == "none":
        return None
    return mode


def _run_sandboxed(
    argv: "list[str]", mode: str, *, input: "bytes | None" = None
) -> "subprocess.CompletedProcess[bytes] | None":
    """*argv* run inside the agent's sandbox at *mode*, its output as bytes; None on a spawn failure."""
    from kiro_crew import sandbox_rlimit, sandbox_spawn

    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.path.expanduser("~")}
    if IS_WINDOWS:
        for key in ("SYSTEMROOT", "USERPROFILE", "PROGRAMDATA"):
            if key in os.environ:
                env[key] = os.environ[key]
    cleanup: "str | None" = None
    try:
        wrapped, env, cleanup = sandbox_spawn.sandboxed_spawn_argv(argv, mode, env=env)
        return sandbox_rlimit.run_limited(
            wrapped,
            stdin=subprocess.DEVNULL if input is None else None,
            input=input,
            capture_output=True,
            timeout=_SSH_G_TIMEOUT_SECS,
            env=env,
            check=False,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
        return None
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass


def _read_in_sandbox(path: str, mode: str) -> "bytes | None":
    """Return bounded sandbox-visible bytes, or raise _Unknowable on read failure."""
    sh = platform_compat.trusted_system_bin("sh")
    head = platform_compat.trusted_system_bin("head")
    if not sh or not head:
        raise _Unknowable(path)
    proc = _run_sandboxed(
        [sh, "-c", _READ_SCRIPT, "sh", path, str(_CONFIG_BYTES_MAX + 1), head], mode
    )
    if proc is None:
        raise _Unknowable(path)
    if proc.returncode == _READ_MISSING and not proc.stdout:
        return None
    if proc.returncode != 0:
        raise _Unknowable(path)
    return bytes(proc.stdout)


def _glob_in_sandbox(pattern: str, mode: str) -> "list[str]":
    """Return sorted sandbox-visible glob matches; a literal path returns itself."""
    if not any(c in pattern for c in _GLOB_CHARS):
        return [pattern]
    sh = platform_compat.trusted_system_bin("sh")
    if not sh:
        raise _Unknowable(pattern)
    proc = _run_sandboxed([sh, "-c", _GLOB_SCRIPT, "sh", pattern], mode)
    if proc is None or proc.returncode != 0:
        raise _Unknowable(pattern)
    try:
        names = bytes(proc.stdout).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _Unknowable(pattern) from exc
    return sorted(name for name in names.split("\0") if name)


def _read_config(
    path: str,
    base: str,
    depth: int,
    blocks: "list[_Block]",
    out: "list[str]",
    mode: str,
    required: bool,
) -> None:
    """Read sandbox-visible bytes into sections and a bounded snapshot.

    Top-level Includes are inlined with Match-all resets. Unsafe Match or
    Include forms raise _Unknowable; a missing required file does too.
    """
    if depth > _INCLUDE_DEPTH_MAX:
        raise _Unknowable(path)
    raw = _read_in_sandbox(path, mode)
    if raw is None:
        if required:
            raise _Unknowable(path)
        return
    if len(raw) > _CONFIG_BYTES_MAX:
        raise _Unknowable(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _Unknowable(path) from exc
    # NUL truncates OpenSSH lines; Python splits more whitespace than OpenSSH.
    if "\x00" in text or any(c.isspace() and c not in " \t\n" for c in text.replace("\r\n", "\n")):
        raise _Unknowable(path)
    if sum(len(line) for line in out) + len(text) > _SNAPSHOT_BYTES_MAX:
        raise _Unknowable(path)
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        if _is_match_exec(stripped):
            raise _Unknowable(path)
        words = _ssh_config_words(stripped)
        keyword = _keyword(stripped)
        if keyword == "match":
            criteria = _ssh_config_words(_bare(stripped))[1:]
            while criteria:
                criterion = criteria.pop(0).lstrip("!")
                if criterion in ("host", "originalhost") and criteria:
                    criteria.pop(0)
                elif criterion not in ("all", "canonical", "final"):
                    raise _Unknowable(path)
            in_block = True
            blocks.append(_Block("match", words[1:]))
        elif keyword == "host":
            in_block = True
            blocks.append(_Block("host", words[1:]))
        elif keyword == "include":
            if in_block:
                raise _Unknowable(path)
            for pattern in words[1:]:
                if "%" in pattern or "$" in pattern:
                    raise _Unknowable(path)
                pattern = os.path.expanduser(pattern)
                if not os.path.isabs(pattern):
                    pattern = os.path.join(base, pattern)
                for target in _glob_in_sandbox(pattern, mode):
                    _read_config(target, base, depth + 1, blocks, out, mode, required=True)
                    out.append(_MATCH_ALL)
                    blocks.append(_Block("all", []))
            continue
        elif keyword in _DESTINATION_KEYS:
            if not blocks:
                blocks.append(_Block("all", []))
            blocks[-1].routes = True
        out.append(line)


def _config_snapshot(mode: str) -> "tuple[list[_Block], str] | None":
    """Read default configs in the sandbox; return None if any is unknowable."""
    blocks: "list[_Block]" = []
    out: "list[str]" = []
    try:
        for path, base in _config_roots():
            if out:
                out.append(_MATCH_ALL)
            blocks.append(_Block("all", []))
            # A fixed default path, so this check names nothing the agent chose.
            required = os.path.lexists(path)
            _read_config(path, base, 0, blocks, out, mode, required)
    except _Unknowable:
        return None
    return blocks, "\n".join(out) + "\n"


def _host_pattern_matches(name: str, pattern: str) -> bool:
    """Match one Host word case-sensitively: only ``*`` and ``?`` are wildcards, as in OpenSSH."""
    return fnmatch.fnmatchcase(name, pattern.replace("[", "[[]"))


def _config_routes(name: str, blocks: "list[_Block]") -> bool:
    """True if a section that applies to *name* sets a destination key.

    Match sections are conservatively assumed to apply, without evaluating them.
    A section counts when it applies to the name as written or lowercased.
    """
    for block in blocks:
        if not block.routes:
            continue
        if block.kind in ("all", "match"):
            return True
        for spelling in {name, name.lower()}:
            if any(
                p.startswith("!") and _host_pattern_matches(spelling, p[1:]) for p in block.patterns
            ):
                continue
            if any(
                not p.startswith("!") and _host_pattern_matches(spelling, p) for p in block.patterns
            ):
                return True
    return False


def _spelling_could_reroute(host: str, blocks: "list[_Block]") -> bool:
    """True if a case or trailing-dot spelling of *host* could take a different route.

    The gate checks only the lowercased, dot-stripped name, but OpenSSH matches
    Host words on the typed spelling. Every spelling must see the same routing
    sections, except one section that applies to *host* itself with nothing
    routing after it (``Host desktop-*`` with a ``ProxyJump``).
    """
    spellings = (host, host + ".")

    def some(p: str) -> bool:
        return any(_host_pattern_matches(s, p.lower()) for s in spellings)

    def every(p: str) -> bool:
        return not any(c.isalpha() for c in p) and all(
            _host_pattern_matches(s, p) for s in spellings
        )

    partial = False
    for block in blocks:
        if not block.routes:
            continue
        if block.kind != "host":
            if partial or block.kind == "match":
                return True
            continue
        positive = [p for p in block.patterns if not p.startswith("!")]
        negated = [p[1:] for p in block.patterns if p.startswith("!")]
        if not any(some(p) for p in positive) or any(every(p) for p in negated):
            continue
        if any(every(p) for p in positive) and not any(some(p) for p in negated):
            if partial:
                return True
            continue
        if partial or not _config_routes(host, [block]):
            return True
        partial = True
    return False


def _addresses(name: str) -> "list[ipaddress.IPv4Address | ipaddress.IPv6Address] | None":
    """The addresses DNS gives *name*: empty for "no such name", None on any other failure."""
    from .argv_floor_hosts import _NO_SUCH_NAME_ERRNOS

    try:
        infos = socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return [] if exc.errno in _NO_SUCH_NAME_ERRNOS else None
    except (OSError, UnicodeError):
        return None
    out = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError:
            return None
        mapped = getattr(ip, "ipv4_mapped", None)
        out.append(mapped if mapped is not None else ip)
    return out


def _hop_host(word: str) -> str:
    """A single literal ``[user@]host[:port]`` hop; empty when not understood."""
    if word.count("@") > 1:
        return ""
    if "@" in word:
        user, word = word.split("@")
        if not user or any(not (c.isascii() and (c.isalnum() or c in "_.-")) for c in user):
            return ""
    if "[" in word or "]" in word:
        # OpenSSH matches Host blocks on the bracketed spelling; not proven here.
        return ""
    if word.count(":") == 1:
        word, port = word.split(":")
        if not port.isdigit():
            return ""
    return word


def _is_literal_host(host: str) -> bool:
    """True for a nonempty hostname or IP with no shell syntax or leading option."""
    return (
        bool(host)
        and not host.startswith("-")
        and all(c.isascii() and (c.isalnum() or c in "_.-:") for c in host)
    )


def _hop_is_remote(host: str, blocks: "list[_Block]") -> bool:
    """Prove one literal destination through the floor's live IP-host guard."""
    from .argv_floor_hosts import _host_is_self

    if not _is_literal_host(host) or _host_is_self(host.lower(), dns_fallback=False):
        return False
    if _config_routes(host, blocks):
        return False
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        addrs = _addresses(host) or []
    return bool(addrs) and all(not _host_is_self(str(ip)) for ip in addrs)


def _ssh_w_host(value: str) -> str:
    """The host in exactly ``ssh [-q] -W %h:%p host``, with either option order."""
    words = value.split()
    if not words or words[0] != "ssh":
        return ""
    if words.count("-q") == 1:
        words.remove("-q")
    if len(words) != 4:
        return ""
    if words[1:3] == ["-W", "%h:%p"]:
        target = words[3]
    elif words[2:] == ["-W", "%h:%p"]:
        target = words[1]
    else:
        return ""
    if target.count(":") == 1 or ("]" in target and not target.endswith("]")):
        return ""
    return _hop_host(target)


def _ssh_g(host: str, snapshot: str, mode: str) -> "dict[str, str] | None":
    """Parse route keys from sandboxed ``ssh -G``, with checked config bytes on stdin.

    None when it did not answer or the platform has no /dev/stdin.
    """
    if IS_WINDOWS or _has_match_exec(snapshot):
        return None
    ssh = platform_compat.trusted_system_bin("ssh")
    if not ssh:
        return None
    proc = _run_sandboxed(
        [ssh, "-G", "-F", "/dev/stdin", "--", host], mode, input=snapshot.encode("utf-8")
    )
    if proc is None or proc.returncode != 0:
        return None
    route: "dict[str, str]" = {}
    for line in bytes(proc.stdout).decode("utf-8", "replace").splitlines():
        key, _, value = line.strip().partition(" ")
        key = key.lower()
        if key in _ROUTE_KEYS and key not in route:
            route[key] = value.strip()
    return route


def route_is_not_self(host: str) -> bool:
    """True only when ssh's effective config proves no hop to *host* is this machine."""
    try:
        # No sandbox to confine the reads and ssh -G: nothing is read or run.
        mode = _sandbox_mode()
        if mode is None or platform_compat.trusted_system_bin("ssh") is None:
            return False
        scanned = _config_snapshot(mode)
        if scanned is None:
            return False
        blocks, snapshot = scanned
        if _spelling_could_reroute(host, blocks):
            return False
        route = _ssh_g(host, snapshot, mode)
        if route is None or not route.get("hostname"):
            return False
        hostname = route["hostname"]
        proxyjump = route.get("proxyjump", "")
        proxycommand = route.get("proxycommand", "")
        has_jump = proxyjump.lower() not in ("", "none")
        has_command = proxycommand.lower() not in ("", "none")
        if has_jump or has_command:
            from .argv_floor_hosts import _host_is_self

            # A known own name refuses even when only the hop's DNS resolves it.
            if not _is_literal_host(hostname) or _host_is_self(
                hostname.lower(), dns_fallback=False
            ):
                return False
            try:
                addrs = [ipaddress.ip_address(hostname)]
            except ValueError:
                # A temporary or unknown resolver failure proves nothing; refuse.
                found = _addresses(hostname)
                if found is None:
                    return False
                addrs = found
            if any(_host_is_self(str(ip)) for ip in addrs):
                return False
        if has_jump:
            return not has_command and _hop_is_remote(_hop_host(proxyjump), blocks)
        if has_command:
            return _hop_is_remote(_ssh_w_host(proxycommand), blocks)
        return hostname.lower() != host.lower() and _hop_is_remote(hostname, blocks)
    except Exception:
        return False
