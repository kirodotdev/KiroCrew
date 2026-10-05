"""pi, launched one process per session by ``AcpClient`` through the pi-acp adapter.

TWO components, and the split is the whole shape of this harness: ``pi-acp`` is a
third-party Node stdio adapter that serves ACP and spawns the ``pi`` coding agent as
``pi --mode rpc --no-themes``; ``pi`` itself has no ``acp`` subcommand. Either can be
absent on its own, so the resolver, the probe and the not-found message each name
both.

pi runs no permission gate of its own, so Crew loads one INTO it: the shipped gate
extension is verified against its pinned digest and sealed into an owner-only
artifact directory, a launcher the adapter is told to run in place of ``pi``
appends it, and a read-back child asks pi's own command registry whether the sealed
copy loaded. A session that cannot establish that is refused before its process
starts. The artifact directory and the seal are shared with the DeepSeek Harness,
whose gate plugin rides the same machinery (:mod:`kiro_crew.acp.harness.deepseek`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import stat
import subprocess as subprocess_mod
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path

from kiro_crew import acp_tool_gate, agent_sdk, platform_compat
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.harness.base import ProcessAdapter, ProcessSession, SpawnContext, SpawnPlan
from kiro_crew.acp.transport_errors import AcpError, AcpToolGateUnroutable, PiGateExtensionTampered
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_NODE_ADAPTER_PACKAGES,
    ACP_BACKEND_PI,
    ACP_BACKEND_PROCESS_NAMES,
    NODE_ADAPTER_ENTRY_SEGMENTS,
)
from kiro_crew.config.paths import config_dir
from kiro_crew.env import describe_search_path
from kiro_crew.json_line import parse_json_object_line
from kiro_crew.sandbox import scrub_agent_subprocess_env, wrap_argv, wrap_argv_async

# The client's logger name: these lines have always been filed under it.
logger = logging.getLogger("kiro_crew.acp.client")

__all__ = [
    "PI_ACP_BIN",
    "PI_ACP_NPM_PKG",
    "PI_BIN",
    "PI_GATE_EXTENSION_SHA256",
    "PI_INSTALL_COMMAND",
    "PI_MIN_VERSION",
    "PI_NPM_PKG",
    "PiLaunch",
    "pi_gate_extension_path",
]


PI_ACP_BIN = ACP_BACKEND_PROCESS_NAMES[ACP_BACKEND_PI]

PI_ACP_NPM_PKG = ACP_BACKEND_NODE_ADAPTER_PACKAGES[ACP_BACKEND_PI]

_PI_ACP_PKG_ENTRY = Path(PI_ACP_NPM_PKG, *NODE_ADAPTER_ENTRY_SEGMENTS)

# The adapter imports @agentclientprotocol/sdk like every Node adapter, so an entry
# script without the hoisted dependency dies at ESM import -- after the spawn.
_PI_ACP_DEP_MARKER = launch_mod.ACP_SDK_DEP_MARKER

# Explicit adapter override, spelled the way the two sibling adapters spell theirs.
_ENV_PI_ACP_BIN = "PI_ACP_BIN"

PI_BIN = "pi"

PI_NPM_PKG = "@earendil-works/pi-coding-agent"

# The adapter's OWN override for the agent executable it spawns. Read here as the
# operator's choice of ``pi`` binary, and then SET in the child's environment to
# Crew's gate launcher, which execs that choice with the extension flag appended.
_ENV_PI_ACP_PI_COMMAND = "PI_ACP_PI_COMMAND"

PI_INSTALL_COMMAND = f"npm i -g {PI_ACP_NPM_PKG} {PI_NPM_PKG}"

# What the adapter passes when it spawns the agent, verbatim from its source. The
# read-back runs the gate launcher with exactly these so what it observes is the
# process the session will be served by.
_PI_RPC_ARGS = ("--mode", "rpc", "--no-themes")

_PI_EXTENSION_FLAG = "--extension"

# The RPC verb whose answer IS the read-back: pi lists every command each loaded
# extension registered, with the file it came from.
_PI_READBACK_REQUEST = {"type": "get_commands", "id": "kiro-crew-gate-readback"}

# Bounded like the opencode read-back; measured at ~0.5s on a loaded dev desktop.
_PI_READBACK_TIMEOUT_S = 30.0

# The oldest ``pi`` the adapter can drive. pi-acp sends RPC commands that older
# releases do not have, and it does not check the version itself. Driven against
# a local model: pi-acp 0.0.34 fails ``session/new`` on pi 0.80.x ("Unknown
# command: get_available_thinking_levels") and waits forever on 0.73.1, and
# pi-acp 0.0.33 never ends a turn on pi 0.80.3 or older. Both drive 0.81.0,
# which is the floor pi-acp 0.0.34 documents. Without this check the chat just
# spins, because nothing below names the version as the cause.
PI_MIN_VERSION = (0, 81, 0)

# Every npm name pi has shipped under. The old one stopped at 0.73.1, so an
# install made under it is always below the floor; it is named so that install
# is recognised and refused rather than read as "version unknown".
_PI_NPM_PACKAGE_NAMES = frozenset({PI_NPM_PKG, "@mariozechner/pi-coding-agent"})

# How far up from the resolved executable to look for pi's package.json.
_PI_MANIFEST_SEARCH_DEPTH = 4

# The per-session nonce the gate extension reads and echoes in its dialogs, so
# the dispatch parser accepts only envelopes this session's own extension wrote.
_ENV_PI_GATE_SESSION = "KIROCREW_PI_GATE_SESSION"

# SHA-256 of the shipped gate extension. The file lives in the package tree,
# which on a source or user install an agent's own file tools may be able to
# write; a read-back that matched the probe by name and path alone would accept
# a rewritten gate. So the packaged bytes are verified against this digest at
# every spawn, copied into the owner-only sandbox run directory, and THAT copy is
# what the harness loads and what the read-back must name. Pinned by
# ``test_acp_pi_backend``, so editing the extension is a deliberate two-file edit.
# The digest is over the LF form of the bytes (``_pi_gate_extension_bytes``): the
# file is text, and a Windows checkout with ``core.autocrlf`` rewrites it CRLF, so a
# digest over the raw bytes would read every Windows install as tampered and refuse
# every pi session there. ``.gitattributes`` pins the checkout LF as well; the
# normalization here is what keeps the property from resting on a repo-config line.
PI_GATE_EXTENSION_SHA256 = "33caa696e70e3b0c0793a705b0c6e06c372c52478e3ac4abf75f0e8d67d1e600"

_pi_acp_argv_cache: tuple[list[str] | None, str] | object = launch_mod._UNRESOLVED

_pi_bin_cache: tuple[str | None, str] | object = launch_mod._UNRESOLVED

_pi_gate_launcher_cache: dict[tuple[str, str], str] = {}


def _resolve_pi_acp_bin() -> tuple[list[str] | None, str]:
    """Find the pi-acp Node entry script and the PATH searched for it.

    The shared ladder with this adapter's parameters; ``PI_ACP_BIN`` is the
    override.
    """
    return launch_mod._resolve_node_adapter_argv(
        bin_name=PI_ACP_BIN,
        override_env=_ENV_PI_ACP_BIN,
        vendored_entry=lambda: launch_mod._vendored_adapter_entry(
            _PI_ACP_PKG_ENTRY, _PI_ACP_DEP_MARKER
        ),
    )


def _resolve_pi_bin() -> tuple[str | None, str]:
    """Find the ``pi`` agent executable and the PATH searched for it.

    The plain-binary ladder (``acp.launch._resolve_self_served_bin``'s shape). The override rung
    is the ADAPTER'S variable: an operator who told pi-acp which ``pi`` to run has
    made the choice this resolver exists to honour, and the gate launcher execs
    exactly that binary -- so setting the variable on the child to the launcher
    does not lose the operator's choice, it wraps it.
    """
    # Read through the client at call time: its unit tests rebind
    # ``kiro_crew.acp.client.subprocess_mod`` under ``_mise_which``, and the client's
    # ``augmented_path`` for the directories searched.
    from kiro_crew.acp.client import _mise_which, augmented_path

    search_path = augmented_path(os.environ.get("PATH", ""))

    override = os.environ.get(_ENV_PI_ACP_PI_COMMAND)
    if override:
        if platform_compat.is_executable_file(override):
            return launch_mod._normalize_exe_casing(override) or override, search_path
        on_path = shutil.which(override, path=search_path)
        if on_path:
            return launch_mod._normalize_exe_casing(on_path) or on_path, search_path

    mise_resolved = _mise_which(PI_BIN)
    if mise_resolved:
        return mise_resolved, search_path

    on_path = shutil.which(PI_BIN, path=search_path)
    if on_path:
        return launch_mod._normalize_exe_casing(on_path) or on_path, search_path

    return None, search_path


def _pi_installed_version(pi_bin: str) -> tuple[tuple[int, ...], str] | None:
    """``(version, npm package name)`` of the pi install *pi_bin* runs, or ``None``.

    Read from the npm package's own ``package.json`` rather than by running
    ``pi --version``: a few small file reads instead of a second child
    process on every spawn. On POSIX the npm bin link resolves into the package,
    so the manifest is a few directories above it. On Windows the bin is a
    ``pi.cmd`` shim: a global one sits in the npm prefix, with the package under
    that directory's ``node_modules``, and a project-local one sits in
    ``node_modules/.bin``, beside the package. Only a manifest carrying one of pi's own
    package names counts. Anything else (a wrapper script, a standalone
    build) answers ``None``, and the caller lets that through.

    Blocking (reads files); callers run it off the loop.
    """
    try:
        here = Path(os.path.realpath(pi_bin)).parent
    except (OSError, ValueError):
        return None
    shim_roots = [here / "node_modules"]
    if here.name == ".bin":
        shim_roots.append(here.parent)
    candidates = [
        root / name / "package.json" for root in shim_roots for name in _PI_NPM_PACKAGE_NAMES
    ]
    for directory in [here, *here.parents][:_PI_MANIFEST_SEARCH_DEPTH]:
        candidates.append(directory / "package.json")
    for manifest in candidates:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("name") not in _PI_NPM_PACKAGE_NAMES:
            continue
        match = re.match(r"(\d+)\.(\d+)\.(\d+)", str(data.get("version") or ""))
        if not match:
            return None
        return tuple(int(part) for part in match.groups()), str(data["name"])
    return None


def _pi_version_issue(pi_bin: str) -> str:
    """Why *pi_bin* is too old for the adapter, or ``""`` when it is not known to be.

    Blocking (see :func:`_pi_installed_version`); callers run it off the loop.
    """
    installed = _pi_installed_version(pi_bin)
    if installed is None or installed[0] >= PI_MIN_VERSION:
        return ""
    version, package = installed
    found = ".".join(str(part) for part in version)
    floor = ".".join(str(part) for part in PI_MIN_VERSION)
    # The two names both install a ``pi`` bin, so npm refuses the new one while
    # the old one is still there ("File exists"). Removing it comes first.
    remove = f"'npm rm -g {package}', then " if package != PI_NPM_PKG else ""
    return (
        f"{PI_BIN} {found} at {pi_bin} is too old for the {PI_ACP_BIN} adapter, which "
        f"needs {PI_BIN} {floor} or newer: on older releases a chat fails or never answers. "
        f"Update it: run {remove}'npm i -g {PI_NPM_PKG}', then start a new chat."
    )


def pi_gate_extension_path() -> str:
    """The absolute path of the gate extension Kiro Crew ships for pi.

    Package data beside :mod:`kiro_crew.agent_sdk`, resolved from that package's
    own location so a wheel install and a source checkout name the same file the
    same way. Returned as a string because it is handed to a shell launcher and
    compared byte-for-byte against what the harness reports back.
    """
    return str(
        Path(agent_sdk.__file__).resolve().parent
        / "gate_extensions"
        / "pi"
        / "kiro_crew_tool_gate.ts"
    )


def _pi_gate_artifact_dir() -> str:
    """Create the owner-only pi gate artifact directory, or refuse the session.

    The launcher and sealed extension are the compensating control that makes this
    harness enforced. They therefore live in a dedicated directory containing no
    credentials, under a real owner-only leaf that cannot fall back to a shared
    temporary directory. Symlinks and Windows junctions are refused because either
    can redirect writes into an agent-chosen location. Blocking (creates and validates
    the directory); callers run it off the loop.

    This is where the leaf is materialized and where its no-follow check lives, rather
    than on ``sandbox``'s shared sealable-ceiling lists, because that walk runs on every
    Linux spawn whatever the backend is: an entry there would let this adapter's
    directory refuse an unrelated session. Every pi spawn reaches this function before
    the sandbox is built, so the read-only seal still finds a directory to bind.
    """
    lexical_home = os.path.abspath(os.path.normpath(str(config_dir())))
    expected = os.path.join(lexical_home, "pi-gate")
    canonical_expected = os.path.join(os.path.realpath(lexical_home), "pi-gate")
    try:
        os.makedirs(expected, mode=0o700, exist_ok=True)
        info = os.lstat(expected)
        if not stat.S_ISDIR(info.st_mode) or platform_compat.is_link_or_junction(expected):
            raise OSError("path is not a real directory")
        if not platform_compat.IS_WINDOWS:
            os.chmod(expected, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
            info = os.stat(expected)
        resolved = os.path.realpath(expected)
    except OSError as exc:
        raise AcpToolGateUnroutable(
            f"{acp_tool_gate.label_for(ACP_BACKEND_PI)} routes tool calls through a gate "
            "extension Kiro Crew seals into its own artifact directory, but that "
            f"directory could not be created or secured ({expected}: {exc}). Fix the "
            "permissions or free space under the Kiro Crew config directory to select "
            "this harness."
        ) from exc
    owner_only = platform_compat.IS_WINDOWS or stat.S_IMODE(info.st_mode) == 0o700
    if resolved != canonical_expected or not owner_only:
        raise AcpToolGateUnroutable(
            f"{acp_tool_gate.label_for(ACP_BACKEND_PI)} routes tool calls through a gate "
            "extension Kiro Crew seals into its own artifact directory, but that "
            f"directory is not a real owner-only leaf ({expected}). Fix its permissions "
            "to select this harness."
        )
    return expected


def _pi_gate_extension_bytes(payload: bytes) -> bytes:
    """*payload* in the one form the digest is pinned over: LF line endings.

    Read in binary and normalized here rather than trusted as it arrived, so the
    verified bytes -- and the sealed copy the harness loads -- are the same on a
    checkout that rewrote the file CRLF as on one that did not. Only ``\\r\\n``
    is folded; any other byte difference is a real difference and fails the digest.
    """
    return payload.replace(b"\r\n", b"\n")


def _seal_pi_gate_extension() -> str:
    """Verify the shipped extension and return the path of a sealed copy to load.

    Reads the packaged file, refuses unless its SHA-256 is
    :data:`PI_GATE_EXTENSION_SHA256`, and writes the verified bytes to a read-only
    file in the owner-only pi gate artifact directory -- the directory the agent's
    file tools are fenced from and every sandbox tier exposes for exec. The copy is
    rewritten whenever its bytes differ from the verified ones, so a copy touched
    between spawns is replaced rather than loaded. Cached per process and inputs
    like the launcher.

    Blocking (reads and may write a file); callers run it off the loop.
    """
    return _seal_gate_extension(
        pi_gate_extension_path(),
        PI_GATE_EXTENSION_SHA256,
        artifact_dir=_pi_gate_artifact_dir(),
        sealed_name=f"kirocrew_pi_gate_{os.getpid()}.ts",
        stage_prefix=f"kirocrew_pi_gate_{os.getpid()}_",
        label="pi gate extension",
    )


def _seal_gate_extension(
    source: str,
    pinned_digest: str,
    *,
    artifact_dir: str,
    sealed_name: str,
    stage_prefix: str,
    label: str,
) -> str:
    """Verify one shipped gate file against its pinned digest and publish a sealed copy.

    The one seal for both gate-extension harnesses: read the packaged bytes, bring
    them to the LF form the digest is pinned over (:func:`_pi_gate_extension_bytes`),
    refuse on any mismatch, and publish them read-only into *artifact_dir* -- the
    owner-only gate artifact directory each writer resolves through the strict
    :func:`_pi_gate_artifact_dir` -- as *sealed_name* through
    :func:`_publish_gate_artifact`. *label* names the file in the refusal, which is
    the same refusal for a file that cannot be read as for one with the wrong bytes:
    no gate this build shipped, no session. Blocking; callers run it off the loop.
    """
    try:
        with open(source, "rb") as fh:
            payload = _pi_gate_extension_bytes(fh.read())
    except OSError as exc:
        raise PiGateExtensionTampered(
            f"the {label} at {source} cannot be read ({exc}); a session cannot "
            "start on a gate whose code this build did not ship. Reinstall Kiro Crew."
        ) from exc
    digest = hashlib.sha256(payload).hexdigest()
    if digest != pinned_digest:
        raise PiGateExtensionTampered(
            f"the {label} at {source} does not match the digest this build "
            f"pinned ({digest[:12]}… vs {pinned_digest[:12]}…); a session "
            "cannot start on a gate whose code this build did not ship. Reinstall Kiro Crew."
        )
    return _publish_gate_artifact(artifact_dir, sealed_name, payload, stage_prefix=stage_prefix)


def _publish_gate_artifact(
    artifact_dir: str, name: str, payload: bytes, *, stage_prefix: str
) -> str:
    """Land *payload* as the read-only file *name* in *artifact_dir*; return its path.

    The write-if-changed tail every gate-artifact writer shares: a file already
    holding exactly these bytes is returned as is (the artifacts are written once
    per gateway process and reused by every later spawn), otherwise the bytes are
    staged under *stage_prefix* in the same directory, made read-only where the
    mode means something (``chmod`` is inert on Windows, where the directory's
    owner-only DACL is the seal), and moved into place atomically. A failed stage is
    removed rather than left for the sweep. *stage_prefix* is caller-named because
    the leaf sweep (``sandbox._PI_GATE_DIR_ARTIFACTS``) reclaims by family, and each
    writer's stage spelling is registered there.
    """
    target = os.path.join(artifact_dir, name)
    try:
        with open(target, "rb") as fh:
            if fh.read() == payload:
                return target
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=artifact_dir, prefix=stage_prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        if not platform_compat.IS_WINDOWS:
            os.chmod(tmp, 0o400)
        os.replace(tmp, target)
    except OSError:
        with suppress(OSError):
            os.remove(tmp)
        raise
    return target


def _pi_gate_launcher_body(pi_bin: str, extension_path: str) -> str:
    """The launcher pi-acp is told to run in place of ``pi``.

    It forwards every argument the adapter passes and appends the extension flag,
    so the harness process is the one the adapter meant to start plus Crew's gate.
    A shell script on POSIX; a ``.cmd`` on Windows, where the adapter itself uses a
    shell for exactly that extension.
    """
    if platform_compat.IS_WINDOWS:
        return f'@echo off\r\n"{pi_bin}" %* {_PI_EXTENSION_FLAG} "{extension_path}"\r\n'
    return (
        "#!/bin/sh\n"
        f'exec {shlex.quote(pi_bin)} "$@" {_PI_EXTENSION_FLAG} {shlex.quote(extension_path)}\n'
    )


def _ensure_pi_gate_launcher(pi_bin: str, extension_path: str) -> str:
    """Write (once per process and inputs) the launcher and return its path.

    Lives in the owner-only pi gate artifact directory, which the sandbox exposes
    read-only because the child has to exec this launcher and read the sealed gate
    extension. Written under a unique ``mkstemp`` name
    that is published to the cache only after the write and the mode change have
    finished, so a concurrent spawn never reads a half-written file, and cached so
    N sessions share one launcher rather than leaving N files behind.

    Blocking (writes a file); callers run it off the loop.
    """
    key = (pi_bin, extension_path)
    cached = _pi_gate_launcher_cache.get(key)
    if cached and os.path.isfile(cached):
        return cached
    artifact_dir = _pi_gate_artifact_dir()
    suffix = ".cmd" if platform_compat.IS_WINDOWS else ".sh"
    fd, tmp = tempfile.mkstemp(
        dir=artifact_dir, prefix=f"kirocrew_pi_gate_{os.getpid()}_", suffix=suffix
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(_pi_gate_launcher_body(pi_bin, extension_path))
        if not platform_compat.IS_WINDOWS:
            os.chmod(tmp, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
    except OSError:
        with suppress(OSError):
            os.remove(tmp)
        raise
    _pi_gate_launcher_cache[key] = tmp
    return tmp


def _pi_readback_remedy() -> str:
    """What an operator does when the harness's command registry cannot be read."""
    return (
        f"Run '{PI_BIN} {' '.join(_PI_RPC_ARGS)}' in the session's working directory "
        'and send it {"type": "get_commands"} on stdin to see what fails, and '
        f"reinstall with '{PI_INSTALL_COMMAND}' if the agent itself is broken."
    )


def _same_file_spelling(path: str) -> str:
    """One spelling per file: symlinks resolved, case folded where the OS does."""
    return os.path.normcase(os.path.realpath(path))


def _same_file_spelling_all(commands: object) -> object:
    """*commands* with every ``sourceInfo.path`` in :func:`_same_file_spelling`.

    Shape-preserving: anything that is not a list of dicts with a string path is
    returned as it came, so the decision module still sees -- and refuses -- an
    unparseable registry as such.
    """
    if not isinstance(commands, list):
        return commands
    out: list = []
    for entry in commands:
        if isinstance(entry, dict):
            info = entry.get("sourceInfo")
            if isinstance(info, dict):
                path = info.get("path")
                if isinstance(path, str) and path:
                    entry = {**entry, "sourceInfo": {**info, "path": _same_file_spelling(path)}}
        out.append(entry)
    return out


def _pi_commands_from_readback(stdout: str) -> object:
    """The ``commands`` list out of pi's ``get_commands`` response, or ``None``.

    pi writes one JSON object per line and other extensions may write UI requests
    before the response, so the lines are scanned for the one answering Crew's
    request id rather than the first parsed.
    """
    want = _PI_READBACK_REQUEST["id"]
    for line in stdout.splitlines():
        frame = parse_json_object_line(line)
        if frame is None or frame.get("id") != want:
            continue
        if frame.get("type") != "response" or frame.get("success") is not True:
            return None
        data = frame.get("data")
        commands = data.get("commands") if isinstance(data, dict) else None
        return commands if isinstance(commands, list) else None
    return None


def _verify_pi_gate(
    session: ProcessSession, backend: str, argv: list[str], extension_path: str
) -> tuple[str, str]:
    """Ask the harness's own command registry whether Crew's gate extension loaded.

    The half that makes this routing VERIFIED. *argv* is the gate launcher plus
    the exact arguments the adapter passes, already sandbox-wrapped by the caller,
    so the process asked is the process the session will be served by. It is
    sent one ``get_commands`` request on stdin and its stdout is read for the
    answer; the extension's probe command must be listed AND sourced from
    *extension_path*, the file Crew shipped. pi exits when stdin closes, so the
    child is short-lived by construction and the timeout is a backstop.

    What this does NOT establish is that the extension's confirm dialog reaches
    the client per call; that is the adapter's contract, and the frame corpus
    carries the observation of it.

    Returns ``("", "")`` when the gate is loaded, else the issue and the remedy
    that can clear it -- a harness problem (could not run, could not parse) gets
    the harness remedy, a registry that answers without the gate gets the
    gate's.

    Blocking (spawns a short-lived child); callers run it off the loop.
    """
    # The session's own credential repair and PATH augmentation, owned by the client
    # and read there at call time, so the read-back's environment is built by the
    # same bindings as the spawn's.
    from kiro_crew.acp.client import _resolve_spawn_env, augmented_path

    # The SAME environment the spawn builds, scrubbed the same way and for the same
    # reasons as the opencode read-back: the per-session overlay is applied because
    # pi reads its agent directory from the environment (``PI_CODING_AGENT_DIR``),
    # and the gateway's own secrets must not reach a foreign binary a few lines
    # ahead of the code that strips them.
    env = scrub_agent_subprocess_env(
        _resolve_spawn_env({**os.environ, **session._extra_env}, kiro_api_key=False)
    )
    env["PATH"] = augmented_path(env.get("PATH", ""))
    # Offline for the read-back only: pi's startup network work (update checks,
    # package refresh) has no bearing on which extensions loaded, and a probe
    # that waits on the network is a probe that can stall the spawn.
    env["PI_OFFLINE"] = "1"
    try:
        completed = subprocess_mod.run(
            argv,
            cwd=session._spawn_work_dir,
            env=env,
            input=json.dumps(_PI_READBACK_REQUEST) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PI_READBACK_TIMEOUT_S,
        )
    except (OSError, subprocess_mod.SubprocessError) as exc:
        return (
            f"the harness's command registry could not be read back ({exc})",
            _pi_readback_remedy(),
        )
    commands = _pi_commands_from_readback(completed.stdout)
    if commands is None:
        detail = f"exit {completed.returncode}" if completed.returncode != 0 else "no response"
        # WHICH fault the child hit. The launcher is /bin/sh exec'ing the resolved
        # harness binary, so its stderr is what separates an exec the OS refused
        # from a shebang that cannot be resolved -- a distinction the exit code
        # alone cannot carry.
        detail = launch_mod._readback_detail_with_diagnosis(detail, completed.stderr)
        return (
            f"the harness's command registry could not be read back ({detail})",
            _pi_readback_remedy(),
        )
    # Same FILE, not same string. pi reports the path it loaded from in its own
    # spelling -- Node's realpath through a symlinked install, a Windows drive
    # letter or 8.3 short form in another case -- and the decision module compares
    # strings without touching the filesystem (it may be called on the loop). So
    # both sides are brought to one spelling here, off the loop, and the sealed
    # copy is still the only file that passes.
    issue = acp_tool_gate.gate_extension_issue(
        backend, _same_file_spelling_all(commands), _same_file_spelling(extension_path)
    )
    if issue:
        return issue, acp_tool_gate.remediation_for(backend)
    return "", ""


class PiLaunch(ProcessAdapter):
    """pi: two components, and a gate extension Crew seals into it and reads back."""

    backend = ACP_BACKEND_PI

    def __init__(self) -> None:
        # The launcher the read-back verified, and the nonce its dialogs echo --
        # carried from the read-back to the child's environment.
        self._launcher = ""
        self._nonce = ""

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Resolve both components, check pi's version, then seal and verify the gate."""
        global _pi_acp_argv_cache, _pi_bin_cache  # noqa: PLW0603
        session = ctx.session
        assert session is not None, "pi is launched for one session"
        # Two components, resolved separately because either can be absent on its
        # own and the not-found message must name the one that is.
        cached_pi_acp: tuple[list[str] | None, str] | object = _pi_acp_argv_cache
        if cached_pi_acp is launch_mod._UNRESOLVED:
            # Both halves are fenced on the SAME generation: a clear is per harness
            # and pi keeps two caches under one id, so one bump has to cover both.
            epoch = launch_mod._resolution_epoch(self.backend)
            cached_pi_acp = await asyncio.to_thread(_resolve_pi_acp_bin)
            if launch_mod._resolution_epoch(self.backend) == epoch:
                _pi_acp_argv_cache = cached_pi_acp
        pi_acp_argv, pi_acp_search_path = (
            cached_pi_acp if isinstance(cached_pi_acp, tuple) else (None, "")
        )
        if not isinstance(pi_acp_argv, list) or not pi_acp_argv:
            raise AcpError(
                f"{PI_ACP_BIN} not found "
                f"({describe_search_path(pi_acp_search_path)}). Install both the "
                f"adapter and the agent with '{PI_INSTALL_COMMAND}', or set "
                f"{_ENV_PI_ACP_BIN} to the adapter's entry script. The '{PI_BIN}' "
                f"CLI alone does not serve ACP."
            )
        cached_pi: tuple[str | None, str] | object = _pi_bin_cache
        if cached_pi is launch_mod._UNRESOLVED:
            epoch_pi_bin = launch_mod._resolution_epoch(self.backend)
            cached_pi = await asyncio.to_thread(_resolve_pi_bin)
            if launch_mod._resolution_epoch(self.backend) == epoch_pi_bin:
                _pi_bin_cache = cached_pi
        pi_bin, pi_search_path = cached_pi if isinstance(cached_pi, tuple) else (None, "")
        if not isinstance(pi_bin, str) or not pi_bin:
            raise AcpError(
                f"{PI_BIN} not found ({describe_search_path(pi_search_path)}). The "
                f"{PI_ACP_BIN} adapter is installed but the agent it spawns is not: "
                f"install it with 'npm i -g {PI_NPM_PKG}', or set "
                f"{_ENV_PI_ACP_PI_COMMAND} to the executable."
            )
        # Refused here, before any child starts, because a too-old pi is not
        # refused by anything later: the gate read-back passes on it, and then the
        # adapter either fails session/new with a bare "Unknown command" or waits
        # forever, so the chat spins with no cause named.
        pi_version_issue = await asyncio.to_thread(_pi_version_issue, pi_bin)
        if pi_version_issue:
            raise AcpError(pi_version_issue)
        argv = pi_acp_argv
        spawn_label = launch_mod._adapter_spawn_label(
            argv, PI_ACP_BIN, pkg_entry=_PI_ACP_PKG_ENTRY, override_env=_ENV_PI_ACP_BIN
        )
        # The refuse-then-mask preflight every enforced host runs, keyed on the
        # routing rather than on this harness's identity, and FIRST because the
        # read-back below starts a child of this harness.
        hidden = await launch_mod._run_preflight_bounded(
            launch_mod._sandbox_preflight, self.backend, ctx.sandbox_mode
        )
        expose = acp_tool_gate.adapter_expose_files(self.backend, hidden)
        # The gate, and the READ-BACK that is what this harness's Routing member
        # promises. pi runs no gate of its own, so Crew's extension is loaded into
        # it through a launcher the adapter is told to run in place of ``pi``.
        # Verified against the pinned digest and copied into the run directory
        # first: the launcher names the COPY, and the read-back requires the probe
        # to be sourced from it, so a rewritten package file is refused here rather
        # than loaded. Off-loop: a file read and possibly a write.
        extension_path = await asyncio.to_thread(_seal_pi_gate_extension)
        self._nonce = uuid.uuid4().hex
        # The nonce the dispatch parser accepts gate envelopes under, recorded on
        # the session whose permission frames it vouches for.
        session._pi_gate_nonce = self._nonce
        self._launcher = await asyncio.to_thread(_ensure_pi_gate_launcher, pi_bin, extension_path)
        # Wrapped in the SAME sandbox with the SAME credential mask as the session
        # spawn, for the same reason the opencode read-back is: this child is the
        # agent itself, loading extensions out of the operator's own directories,
        # moments before the masked spawn.
        readback_argv, readback_cleanup = await wrap_argv_async(
            [self._launcher, *_PI_RPC_ARGS],
            mode=ctx.sandbox_mode,
            strip_python_env=True,
            extra_hidden_dirs=hidden,
            extra_expose_files=expose,
            _prepare=wrap_argv,
        )
        try:
            routing_issue, routing_remedy = await asyncio.to_thread(
                _verify_pi_gate, session, self.backend, readback_argv, extension_path
            )
        finally:
            if readback_cleanup:
                await asyncio.to_thread(launch_mod._unlink_readback_launcher, readback_cleanup)
        if routing_issue:
            # Refused before the first prompt: without the extension loaded this
            # harness runs every tool call unasked, so a session that cannot
            # establish it is a session where none of Crew's tool controls run.
            try:
                acp_tool_gate.enforce_runtime_routing(
                    self.backend,
                    routing_issue,
                    remedy=routing_remedy,
                )
            except acp_tool_gate.ToolGateUnroutable as exc:
                raise AcpToolGateUnroutable(str(exc)) from None
        return SpawnPlan(
            argv=argv,
            spawn_label=spawn_label,
            stderr_label=spawn_label,
            extra_hidden_dirs=hidden,
            extra_expose_files=expose,
        )

    def apply_spawn_env(self, env: dict[str, str], *, spawned_binary: str | None = None) -> None:
        """Point the adapter at the verified launcher, and hand pi its nonce.

        The launcher is applied unconditionally: an operator's own value for this
        variable was already honoured by :func:`_resolve_pi_bin` and is what the
        launcher execs. The nonce reaches the pi process through the adapter, which
        spawns it with its own environment; the extension echoes it in every dialog.
        """
        if self._launcher:
            env[_ENV_PI_ACP_PI_COMMAND] = self._launcher
            env[_ENV_PI_GATE_SESSION] = self._nonce
