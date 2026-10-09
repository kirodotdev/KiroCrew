"""The DeepSeek Harness, launched one process per session by ``AcpClient``.

DeepSeek Harness is a plugin host, and ACP is one of the profiles it boots, so its argv
is the harness's own binary plus the profile selector (``dsh --profile acp``), resolved
off the shared self-served ladder. The profile is shipped: it is created on first use,
and both of its bundles are inside the installed package's own dependency closure, so
a global install needs no workspace checkout and no per-profile dependency step.

This harness runs no gate of its own that decides a tool call, so Crew composes its
gate plugin into it through a per-launch ``--patch`` file -- the composition channel
its own launcher documents -- and boots it once in a throwaway probe to read the
plugin's load marker back before the session's process starts. The plugin is sealed
the way pi's gate extension is, into the same owner-only artifact directory
(:mod:`kiro_crew.acp.harness.pi`). The provider key reaches the child from Crew's own
vault as an environment variable, never from a file inside the child's tree.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import shutil
import stat
import subprocess as subprocess_mod
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.request import url2pathname

from kiro_crew import acp_tool_gate, agent_scratch, agent_sdk, platform_compat
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.harness import pi as pi_mod
from kiro_crew.acp.harness.base import ProcessAdapter, ProcessSession, SpawnContext, SpawnPlan
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable
from kiro_crew.agent_sdk.backends import ACP_BACKEND_DEEPSEEK
from kiro_crew.config.paths import config_dir
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.mcp_gateway.secret_uri import SECRET_URI_PREFIX, resolve_secret_uris
from kiro_crew.sandbox import (
    agent_env_scrub_prefixes,
    scrub_agent_subprocess_env,
    wrap_argv,
    wrap_argv_async,
)

# The client's logger name: these lines have always been filed under it.
logger = logging.getLogger("kiro_crew.acp.client")

__all__ = [
    "DEEPSEEK_GATE_EXTENSION_SHA256",
    "DEEPSEEK_PERMISSION_MODE",
    "DeepseekLaunch",
    "deepseek_gate_extension_path",
]


# The DeepSeek Harness half of the same routing member. Its composition takes a
# per-launch patch file (``--patch``, ``packages/boot/cmdline``) whose ``insert``
# row names a plugin by ABSOLUTE PATH, which is how Crew's gate is composed into a
# profile it does not own; the plugin then answers the harness's own
# ``tools/pre-execute`` waterfall. There is no command registry to read back, so
# the plugin reports its load through a marker file at a path named here.
_DSH_PATCH_FLAG = "--patch"

_ENV_DSH_GATE_SESSION = "KIROCREW_DSH_GATE_SESSION"

_ENV_DSH_GATE_MARKER = "KIROCREW_DSH_GATE_MARKER"

# The provider-key NAMES the probe asks the plugin to prove withheld from a harness
# child, ``:``-joined (each is a POSIX identifier, so the separator cannot occur
# inside one). The probe sets each such name to a canary -- this prefix plus the
# probe's nonce -- never to the key: the property "this name does not reach the
# harness's shells" can only be observed for a name that is SET, and the probe boots
# a plugin host that needs no provider key.
_ENV_DSH_GATE_SCRUB_NAMES = "KIROCREW_DSH_GATE_SCRUB_NAMES"

_DSH_GATE_SCRUB_CANARY_PREFIX = "kirocrew-dsh-gate-scrub-canary-"

# One harness boot, bounded like the pi and opencode read-backs; measured at ~3s
# for this harness, which boots a plugin host rather than a single binary, plus the
# plugin's own child spawn for the scrub proof (one ``node -e``, well under a second).
_DSH_GATE_READBACK_TIMEOUT_S = 60.0

# How often the probe looks for the marker while the harness is still up.
_DSH_GATE_MARKER_POLL_S = 0.05

# After the marker is published the harness is told to exit (stdin EOF) and given
# this long to do so before it is killed; its own bounded shutdown is 5s.
_DSH_GATE_PROBE_EXIT_S = 15.0

# The plugin's real marker is a few hundred bytes. This cap bounds child-controlled
# memory before JSON parsing while leaving ample room for the complete routing snapshot.
_DSH_GATE_MARKER_MAX_BYTES = 64 * 1024

# Same seal, same reason, for the DeepSeek Harness gate plugin. Pinned by
# ``test_acp_deepseek_backend``, so editing the plugin is a deliberate two-file
# edit.
DEEPSEEK_GATE_EXTENSION_SHA256 = "d95796f59d8e30f840dbb12ad4253c18f09e5b3972435e5b5f307d6b7d994351"

# ── deepseek (ACP_BACKEND_DEEPSEEK) ──
# DeepSeek Harness is a plugin host, and ACP is one of the profiles it boots. So the
# argv is the harness's own binary plus the profile selector -- the plain-binary
# ladder again, with no adapter package and no Node entry script to resolve. The
# profile is shipped: it is created on first use, and both of its bundles are inside
# the installed package's own dependency closure, so a global install needs no
# workspace checkout and no per-profile dependency step.
# The one variable the shipped ACP profile composes its whole permission posture
# from: it selects a sandbox mode AND an approval policy together. Pinned so the
# posture never depends on an ambient value. A config layer can set the composed rows
# directly and never read this variable, which weakens confinement -- and changes
# nothing about whether Crew is consulted, because it never is.
_ENV_DEEPSEEK_PERMISSION_MODE = "DSH_PERMISSION_MODE"

# The posture Crew pins: confined to the workspace rather than unconfined. Defence
# in depth and nothing more, because it does not make this harness's tool calls reach
# Crew's gate.
DEEPSEEK_PERMISSION_MODE = "workspace-write"

# ── agent.deepseek_env: the provider key, fed from Crew's vault ──
# This harness's own credential layering resolves a provider key from the INHERITED
# PROCESS ENVIRONMENT first, above both of its credential files
# (``@deepseek-ai/dsh-credentials-local``'s own header: inherited environment,
# read-only and winning, then ``$DSH_HOME/.credentials.yaml``, then ``<cwd>/.env``,
# then ``$DSH_HOME/.env``), and a credential reference in its configuration IS an
# environment-variable name (``@deepseek-ai/dsh-credentials``'s ``credentialRef``).
# So handing the key to the harness PROCESS is authoritative for whichever provider
# names it, which is what lets ``host_auth`` declare no ``adapter_own_leaves`` for this
# harness and leave both credential files masked for its whole process tree.
#
# The reason an env-fed key is SAFER here than a spared file, rather than merely
# equivalent: the harness scrubs its own children. ``@deepseek-ai/dsh-subprocess``
# defines ``SENSITIVE_ENV_PATTERN = /KEY|PASSWORD|SECRET|TOKEN/i`` and its
# ``scrubbedParentEnv()`` drops every inherited variable matching it (plus every
# ``DSH_*`` name) before ANY child spawn -- on both spawn paths, the local bash tool's
# ``spawnSpec`` and the terminal tool's ``childEnvironment``, which both reach the same
# ``childEnv()``. So a key whose NAME is in that class is invisible to the shells the
# model drives. A name OUTSIDE that class is forwarded to those children, which is why
# Crew refuses one rather than injecting it.
#
# This regex is a MIRROR of the harness's, observed at dsh 0.1.5-rc.2, and it is the
# validator's first filter only -- not what the promise rests on. A harness release
# that narrowed or dropped its scrub would forward the key with no in-band signal, so
# the property is PROVED at every spawn instead: the read-back probe sets each
# configured name to a canary, and the gate plugin spawns one trivial child through
# the harness's own subprocess service and records, per name, whether it reached
# that child (``child_env`` in the load marker). The session is refused when any
# did (``acp_tool_gate.gate_marker_issue``).
_DEEPSEEK_ENV_CHILD_SCRUB_CLASS = re.compile("KEY|PASSWORD|SECRET|TOKEN", re.IGNORECASE)

# The harness's own reference grammar: a POSIX shell identifier
# (``@deepseek-ai/dsh-credentials``'s ``credentialRef``). Matched with ``fullmatch``
# rather than a ``$``-anchored pattern, which in Python would also accept a trailing
# newline -- and a name with one is not the variable the operator wrote.
_DEEPSEEK_ENV_NAME_GRAMMAR = re.compile("[A-Za-z_][A-Za-z0-9_]*")

# Namespaces on this child that a provider-key mapping may not enter: the harness's
# own, which it scrubs from its children itself, and Crew's own, which carries this
# session's IDENTITY -- ``KIROCREW_SESSION_KEY`` and the signed stub token
# (``STUB_SESSION_TOKEN_ENV``) are written by ``_apply_session_identity_env`` AFTER
# the provider key is placed, so a mapping onto either name would be overwritten
# by a live Crew credential and the harness would present THAT to its provider.
# Both names are in the harness's scrub class, so nothing else here would refuse
# them. A prefix rather than a list, because Crew's namespace grows.
_DEEPSEEK_ENV_RESERVED_PREFIXES = ("DSH_", "KIROCREW_")

# Names outside those namespaces that Crew still sets on this harness's child, listed
# rather than derived because each is set at a different site and a derivation would
# have to reach all of them: the permission pin, the harness home from
# ``_extra_env`` (both ``DSH_``-prefixed, so already refused; kept as documentation
# of the sites) and kiro-cli's own model credential (actively stripped for a foreign
# backend). An operator mapping one of these would either lose their key or break
# the gate, depending on which write landed last, so the mapping is refused instead.
# ``test_every_scrub_class_name_crew_writes_on_the_child_is_reserved`` derives the
# scrub-class names the spawn pipeline writes and fails when one is missing here.
_DEEPSEEK_ENV_CREW_OWNED_NAMES = frozenset(
    {
        _ENV_DSH_GATE_MARKER,
        _ENV_DSH_GATE_SESSION,
        _ENV_DSH_GATE_SCRUB_NAMES,
        _ENV_DEEPSEEK_PERMISSION_MODE,
        "DSH_HOME",
        "KIRO_API_KEY",
        "KIROCREW_RUNTIME_PYTHON",
        "KIROCREW_SESSION_KEY",
        STUB_SESSION_TOKEN_ENV,
    }
)


def deepseek_gate_extension_path() -> str:
    """The absolute path of the gate plugin Kiro Crew ships for the DeepSeek Harness.

    Package data beside :mod:`kiro_crew.agent_sdk`, resolved the same way
    :func:`kiro_crew.acp.harness.pi.pi_gate_extension_path` resolves its sibling.
    ``.mjs`` rather than the ``.ts`` pi loads: this harness composes a plugin into a
    running Node process from the published build, which resolves a module
    specifier and does not transpile, so the shipped file is the file that runs.
    """
    return str(
        Path(agent_sdk.__file__).resolve().parent
        / "gate_extensions"
        / "deepseek"
        / "kiro_crew_tool_gate.mjs"
    )


def _seal_deepseek_gate_extension() -> str:
    """Verify the shipped gate plugin and return the path of a sealed copy to load.

    The :func:`kiro_crew.acp.harness.pi._seal_pi_gate_extension` contract, for the
    other member of this routing, through the same
    :func:`kiro_crew.acp.harness.pi._seal_gate_extension`: refuse unless the
    packaged bytes match :data:`DEEPSEEK_GATE_EXTENSION_SHA256`, write them into the
    owner-only gate artifact directory, and hand back THAT path -- which is what the
    patch file names and what the load marker must report. Blocking; callers run it
    off the loop.
    """
    return pi_mod._seal_gate_extension(
        deepseek_gate_extension_path(),
        DEEPSEEK_GATE_EXTENSION_SHA256,
        artifact_dir=pi_mod._pi_gate_artifact_dir(),
        sealed_name=f"kirocrew_dsh_gate_{os.getpid()}.mjs",
        stage_prefix=f"kirocrew_dsh_gate_{os.getpid()}_",
        label="DeepSeek Harness gate plugin",
    )


def _write_deepseek_gate_patch(sealed_extension: str) -> str:
    """Write the per-launch patch that composes *sealed_extension*, and return its path.

    Four rows in the same owner-only gate artifact directory the sealed plugin lives
    in. The first is one ``insert`` naming the plugin by absolute path, which is the
    composition channel the harness's own launcher documents (its ``boot/cmdline``
    package, and its own plugin-development guide). Emitted per process rather than
    shipped as package data because it has to name the SEALED copy, whose path
    carries this process's pid.

    The second pins the harness's tool presentation to ``native``, and it is a
    SECURITY row rather than a preference. Under ``ptc`` or ``both`` the harness
    exposes a reserved ``run_code`` transport instead of native tool schemas, and a
    program running inside it reaches Node's own APIs directly -- filesystem,
    network, subprocess -- which are not tool calls and therefore never traverse
    ``tools/pre-execute``. The gate would see one ``run_code`` call it cannot read
    and could apply no command or path rule to the JavaScript inside it. ``native``
    is the harness's own default, so this row changes nothing on a default install;
    what it does is stop an operator layer from selecting a mode that would carry
    side effects around the gate, and a ``--patch`` overlay is applied after the
    profile's own layer, so the pin wins.

    The remaining rows reassert the stock approval service with policy ``ask`` and
    the stock ACP bridge as enabled with its startup dependency. dsh applies this
    overlay after the operator layer, and ``applyEntryPatches`` replaces these fields,
    so disabling either row, changing the approval policy or changing the ACP startup
    dependency does not survive. Its ``name`` is a match guard rather than an
    assignment: a layer that replaced either module is not overwritten, and the gate
    marker's owner read-back then refuses the session.

    The path is quoted with JSON, which is a strict subset of YAML's
    double-quoted scalar, so a directory containing a quote or a backslash
    cannot end the scalar early. Blocking; callers run it off the loop.
    """
    artifact_dir = pi_mod._pi_gate_artifact_dir()
    body = (
        f"- insert:\n    - id: kiro-crew-tool-gate\n      name: {json.dumps(sealed_extension)}\n"
        "- id: tools\n  config:\n    mode: native\n"
        "- id: approval\n"
        "  name: '@deepseek-ai/dsh-user-approval'\n"
        "  disabled: false\n"
        "  config:\n"
        "    policy: ask\n"
        "- id: acp\n"
        "  name: '@deepseek-ai/dsh-acp'\n"
        "  disabled: false\n"
        "  inject:\n"
        "    - acpAppStartup\n"
    )
    return pi_mod._publish_gate_artifact(
        artifact_dir,
        f"kirocrew_dsh_gate_{os.getpid()}.patch.yml",
        body.encode("utf-8"),
        stage_prefix=f"kirocrew_dsh_patch_{os.getpid()}_",
    )


def _validate_deepseek_env_mapping(mapping: dict[str, str]) -> None:
    """Refuse every ``agent.deepseek_env`` entry this harness would not honour.

    Raises :exc:`ValueError` whose message is operator-readable and names ONLY the
    env-var KEY -- never the secret's vault name and never its value. That is the
    same rule :mod:`kiro_crew.mcp_gateway.secret_uri` states for its own refusals,
    and it is what lets this message reach a log and a chat error card unsanitised:
    the key is operator-declared config, and ``!r`` escapes any control character in
    it, so a hostile name has no text to forge.

    Each rule refuses a mapping that would FAIL SILENTLY rather than one that is
    merely unusual, which is why they are refusals and not warnings:

    * a plaintext value would put a live provider key in ``config.json``, which this
      whole route exists to avoid -- the key belongs in the vault;
    * a name outside the harness's POSIX-identifier reference grammar is not a
      credential reference the harness can resolve at all;
    * a name outside the harness's own child-scrub class
      (:data:`_DEEPSEEK_ENV_CHILD_SCRUB_CLASS`) is FORWARDED by the harness into
      every shell it spawns, which hands the model's own bash tool the key -- the
      exact exposure feeding it through the environment exists to close;
    * a ``DSH_``-prefixed name is the harness's reserved namespace, a
      ``KIROCREW_``-prefixed one is Crew's -- it carries this session's identity
      credentials, which are written onto the child AFTER the provider key and
      would replace it, handing the harness's provider a live Crew credential --
      and a name Crew otherwise writes on this child
      (:data:`_DEEPSEEK_ENV_CREW_OWNED_NAMES`) would either lose the operator's
      key or overwrite the gate's own variables, depending on which write landed
      last;
    * a name Crew's own agent environment scrub strips
      (:func:`kiro_crew.sandbox.agent_env_scrub_prefixes`) would be removed on the
      shared spawn tail AFTER this injection, so the harness would start with no key
      and nothing would say why.
    """
    scrub_prefixes = agent_env_scrub_prefixes()
    for key, value in mapping.items():
        if not value.startswith(SECRET_URI_PREFIX):
            raise ValueError(
                f"agent.deepseek_env entry {key!r} holds a literal value. This "
                f"mapping takes a '{SECRET_URI_PREFIX}<vault name>' reference only, "
                "so a provider key is never stored in config.json. Save the key "
                "under Settings > Secrets, then map it as "
                f"'{SECRET_URI_PREFIX}<vault name>'."
            )
        if not _DEEPSEEK_ENV_NAME_GRAMMAR.fullmatch(key):
            raise ValueError(
                f"agent.deepseek_env entry {key!r} is not an environment-variable "
                "name the harness can resolve: its credential references are POSIX "
                "shell identifiers, matching [A-Za-z_][A-Za-z0-9_]*."
            )
        if key.startswith(_DEEPSEEK_ENV_RESERVED_PREFIXES) or key in _DEEPSEEK_ENV_CREW_OWNED_NAMES:
            raise ValueError(
                f"agent.deepseek_env entry {key!r} names a variable Kiro Crew or the "
                "harness sets on this child itself (the DSH_ and KIROCREW_ namespaces, "
                "and Kiro Crew's own session credentials), so the mapping would either "
                "lose the key, overwrite the tool gate's own value, or hand the "
                "harness's provider a Kiro Crew credential. Choose a provider "
                "credential name instead, such as DEEPSEEK_API_KEY."
            )
        if not _DEEPSEEK_ENV_CHILD_SCRUB_CLASS.search(key):
            raise ValueError(
                f"agent.deepseek_env entry {key!r} is outside the name class the "
                "harness withholds from its own shell children (it scrubs every "
                "inherited name matching KEY, PASSWORD, SECRET or TOKEN, "
                "case-insensitively), so the harness would forward this name to "
                "every shell it runs and the model's bash tool could read the key. "
                "Name the credential reference in the harness's provider "
                "configuration with a name in that class, such as DEEPSEEK_API_KEY."
            )
        if any(key.startswith(prefix) for prefix in scrub_prefixes):
            raise ValueError(
                f"agent.deepseek_env entry {key!r} matches a name prefix Kiro Crew's "
                "own agent environment scrub removes before the child starts, so the "
                "injection would be undone and the harness would start with no key. "
                "Choose a provider credential name outside that set."
            )


def _deepseek_vault_env_names() -> tuple[str, ...]:
    """The env-var NAMES ``agent.deepseek_env`` maps, validated, in a stable order.

    What the read-back probe needs and all it may have: it hands each name to the
    gate plugin under a canary value so the plugin can prove the harness withholds
    that name from its shell children, and it boots a third-party plugin host, so it
    is never given the key itself. Same validator as :func:`_deepseek_vault_env`,
    same :exc:`ValueError` on a mapping this harness would not honour; the vault is
    not opened here. Blocking (reads the config file); callers run it off the loop.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    mapping = dict(KiroCrewConfig.load().agent.deepseek_env)
    if not mapping:
        return ()
    _validate_deepseek_env_mapping(mapping)
    return tuple(sorted(mapping))


def _deepseek_vault_env() -> tuple[dict[str, str], tuple[str, ...]]:
    """``agent.deepseek_env`` validated and resolved into child env vars.

    Returns ``(env, secret_keys)``: the variables to place on the harness's child,
    and the keys now holding PLAINTEXT. The contract
    :func:`kiro_crew.mcp_gateway.secret_uri.resolve_secret_uris` states -- clear
    the plaintext from the returned dict as soon as nothing needs it from there
    -- is honoured by the deepseek spawn arm, which empties the dict the
    moment its entries are copied onto the child's env, inside the arm rather than
    on the shared post-spawn path (harness-parity H13).

    Every failure is a :exc:`ValueError`, from this module's validator or from the
    resolver's own fail-closed refusals (a malformed reference, a secret absent from
    the vault). One exception type, because the caller does the same thing with
    both: refuse the session rather than start a harness that cannot reach a model.

    Blocking: reads the config file and the vault, so it runs off the event loop.
    Config is imported lazily for this module's usual reason -- ``config.loader``
    reaches this module through ``acp.session_handle``.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    mapping = dict(KiroCrewConfig.load().agent.deepseek_env)
    if not mapping:
        return {}, ()
    _validate_deepseek_env_mapping(mapping)
    resolved, secret_keys = resolve_secret_uris(
        mapping, Path(config_dir()), subject="agent.deepseek_env"
    )
    return resolved, tuple(sorted(secret_keys))


def _read_deepseek_gate_marker(marker_path: str) -> object:
    """Read one child-written marker without following, blocking, or growing memory.

    The final component is opened through the cross-platform no-reparse helper
    with nonblocking mode, then accepted only as a regular file no larger than
    :data:`_DSH_GATE_MARKER_MAX_BYTES`. The one bounded read asks for one byte
    beyond the fstat size, so truncation or growth before that read is malformed
    rather than a prefix parse. Every refusal returns ``None`` for the existing
    routing-refusal path.
    """
    try:
        fd = platform_compat.open_file_no_reparse(marker_path, nonblocking=True)
    except OSError:
        return None
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size < 0
            or metadata.st_size > _DSH_GATE_MARKER_MAX_BYTES
        ):
            return None
        payload = os.read(fd, metadata.st_size + 1)
        if len(payload) != metadata.st_size:
            return None
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        return json.loads(payload)
    except (ValueError, RecursionError):
        return None


def _verify_deepseek_gate(
    session: ProcessSession,
    backend: str,
    argv: list[str],
    extension_path: str,
    marker_path: str,
    nonce: str,
    *,
    child_scrub_names: tuple[str, ...] = (),
) -> tuple[str, str]:
    """Boot the harness once with the gate composed and read its load marker back.

    The :func:`kiro_crew.acp.harness.pi._verify_pi_gate` contract for the
    :data:`~kiro_crew.agent_sdk.backends.Readback.LOAD_MARKER` style. *argv* is
    the session's own argv -- the harness binary, its profile selector and the
    ``--patch`` that composes the gate -- so what is observed is the
    composition the session will run. The child is given the marker path and
    the nonce; it boots, and once Cordis settles the plugin snapshots the
    routing, proves the child-env scrub on a child of its own, and PUBLISHES the
    marker (written beside its path, renamed onto it).

    stdin is held open until that marker exists and closed only then: stdin EOF
    is this profile's own bounded shutdown (``packages/bundle/acp-app``), and
    that shutdown disposes the subprocess service, which terminates every child
    it still owns -- an EOF handed over at boot would end the proof's child under
    it and refuse every clean session. A harness that publishes nothing within
    :data:`_DSH_GATE_READBACK_TIMEOUT_S` is killed and refused.

    *child_scrub_names* are the ``agent.deepseek_env`` names: each is set in the
    probe's environment to a canary carrying the nonce -- never to the key, which
    this plugin host does not need -- and listed for the plugin, so the marker
    must report exactly that set proved absent from the harness's child.

    Both output streams are discarded: they are plugin-controlled and carry no
    read-back data. The marker is opened without following links or blocking on
    special files, accepted only as a regular file up to 64 KiB, and read once
    with a one-byte growth check before JSON parsing.

    Returns ``("", "")`` only when the marker is the one this session's own
    plugin wrote, its approval snapshot names the stock ACP bridge as the sole
    answerer under the pinned policy, the composed tool presentation is
    ``native``, and every configured name was withheld from the child. Blocking;
    callers run it off the loop.
    """
    # The session's own credential repair and PATH augmentation, owned by the client
    # and read there at call time, so the read-back's environment is built by the
    # same bindings as the spawn's.
    from kiro_crew.acp.client import _resolve_spawn_env, augmented_path

    with suppress(OSError):
        os.unlink(marker_path)
    # Built exactly as the pi probe's is, and NOT from a raw ``os.environ``. Two
    # reasons, both load-bearing. A plugin runs during this boot, so an unscrubbed
    # environment hands a third-party bundle the gateway's own credentials before
    # anything has been verified. And the probe is only evidence about the session
    # if it boots in the session's environment: ``_extra_env`` carries this
    # session's ``DSH_HOME``, so a probe reading the ambient one would compose a
    # different profile from the child it speaks for.
    env = scrub_agent_subprocess_env(
        _resolve_spawn_env({**os.environ, **session._extra_env}, kiro_api_key=False)
    )
    env["PATH"] = augmented_path(env.get("PATH", ""))
    env[_ENV_DSH_GATE_MARKER] = marker_path
    env[_ENV_DSH_GATE_SESSION] = nonce
    env[_ENV_DEEPSEEK_PERMISSION_MODE] = DEEPSEEK_PERMISSION_MODE
    # The canaries, set AFTER the scrub above so nothing strips them: the
    # validator already refused any name Crew's own scrub would take.
    env[_ENV_DSH_GATE_SCRUB_NAMES] = ":".join(child_scrub_names)
    for name in child_scrub_names:
        env[name] = f"{_DSH_GATE_SCRUB_CANARY_PREFIX}{nonce}"
    deadline = time.monotonic() + _DSH_GATE_READBACK_TIMEOUT_S
    try:
        process = subprocess_mod.Popen(
            argv,
            cwd=session._spawn_work_dir,
            env=env,
            stdin=subprocess_mod.PIPE,
            stdout=subprocess_mod.DEVNULL,
            stderr=subprocess_mod.DEVNULL,
        )
    except (OSError, subprocess_mod.SubprocessError) as exc:
        # The boot itself failed, which is not the same finding as a boot that
        # composed no gate -- but it lands in the same place, because a session
        # cannot be started on a gate that was never observed.
        return (
            f"the gate's load marker could not be read back ({exc})",
            acp_tool_gate.remediation_for(backend),
        )
    published = False
    try:
        while time.monotonic() < deadline:
            if os.path.lexists(marker_path):
                published = True
                break
            if process.poll() is not None:
                # The harness ended on its own before publishing: nothing more is
                # coming, and the read below judges whatever it left.
                break
            time.sleep(_DSH_GATE_MARKER_POLL_S)
        with suppress(OSError):
            if process.stdin is not None:
                process.stdin.close()
        try:
            process.wait(timeout=_DSH_GATE_PROBE_EXIT_S if published else 0)
        except subprocess_mod.TimeoutExpired:
            process.kill()
            with suppress(subprocess_mod.SubprocessError, OSError):
                process.wait(timeout=_DSH_GATE_PROBE_EXIT_S)
    except (OSError, subprocess_mod.SubprocessError) as exc:
        with suppress(OSError, subprocess_mod.SubprocessError):
            process.kill()
        return (
            f"the gate's load marker could not be read back ({exc})",
            acp_tool_gate.remediation_for(backend),
        )
    if not published and not os.path.lexists(marker_path):
        return (
            "the gate's load marker was not written within "
            f"{_DSH_GATE_READBACK_TIMEOUT_S:.0f}s of booting the harness, so Kiro "
            "Crew's gate plugin did not load and tools would run unasked",
            acp_tool_gate.remediation_for(backend),
        )
    marker = _read_deepseek_gate_marker(marker_path)
    # Same FILE, not same string: the plugin reports its own module URL as Node
    # resolved it, so both sides are brought to one spelling off the loop before
    # the decision module compares them without touching the filesystem.
    if isinstance(marker, dict):
        module = marker.get("module")
        if isinstance(module, str) and module.startswith("file://"):
            marker = {**marker, "module": pi_mod._same_file_spelling(url2pathname(module[7:]))}
    issue = acp_tool_gate.gate_marker_issue(
        backend,
        marker,
        pi_mod._same_file_spelling(extension_path),
        nonce,
        child_scrub_names=child_scrub_names,
    )
    if issue:
        return issue, acp_tool_gate.remediation_for(backend)
    return "", ""


class DeepseekLaunch(ProcessAdapter):
    """DeepSeek Harness: a plugin host Crew composes its gate into, verified by a probe."""

    backend = ACP_BACKEND_DEEPSEEK

    def __init__(self) -> None:
        # The nonce the probe issued, carried to the session's own child.
        self._nonce = ""

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Resolve the binary, mask it, compose and seal the gate, and prove it loads."""
        session = ctx.session
        assert session is not None, "the DeepSeek Harness is launched for one session"
        _deepseek_bin, argv, spawn_label, stderr_label = (
            await launch_mod.resolve_self_served_launch(self.backend)
        )
        await session._prepare_session_mcp()
        # The refuse-then-mask preflight every ENFORCED harness takes, and FIRST for
        # the reason the pi adapter gives: the read-back below starts a child of this
        # harness, and it must not run outside the mask the session runs under. The
        # mask is gated on ``tool_gate.ENFORCED_ROUTINGS``, which this harness is
        # inside, and NOTHING of its own is spared from it: both of its credential
        # leaves stay masked for the whole process tree, because its provider key
        # arrives as an environment variable from Crew's vault
        # (``agent.deepseek_env``, :meth:`prepare_spawn_env`) rather than from a file
        # the child can open -- ``agent_sdk/host_auth`` declares
        # ``adapter_own_leaves=()`` for it.
        hidden = await launch_mod._run_preflight_bounded(
            launch_mod._sandbox_preflight, self.backend, ctx.sandbox_mode
        )
        expose = acp_tool_gate.adapter_expose_files(self.backend, hidden)
        #
        # The gate, and the READ-BACK that is what this harness's Routing member
        # promises. This harness runs no gate of its own that decides a tool call,
        # so Crew's plugin is composed into it through a per-launch patch -- the
        # composition channel its own launcher documents -- and the plugin answers
        # its ``tools/pre-execute`` waterfall with ``ask``. Verified against the
        # pinned digest and copied into the sealed gate-artifact leaf first (the
        # owner-only directory pi's extension also lives in, read-only against every
        # harness child): the patch names the COPY, and the read-back requires the
        # marker to report it, so a rewritten package file is refused here rather
        # than loaded. The marker itself is NOT written there -- the leaf is sealed
        # against the child -- but into the probe's own throwaway private scratch
        # window below. The probe's is the ONLY marker: the session it speaks for
        # names no marker path, and the plugin skips the write when none is named.
        # Off-loop: file reads and writes.
        extension_path = await asyncio.to_thread(_seal_deepseek_gate_extension)
        gate_patch = await asyncio.to_thread(_write_deepseek_gate_patch, extension_path)
        self._nonce = uuid.uuid4().hex
        # The nonce the permission-frame tripwire keys on, recorded on the session
        # whose frames it judges.
        session._deepseek_gate_nonce = self._nonce
        argv = [*argv, _DSH_PATCH_FLAG, gate_patch]
        spawn_label = " ".join(argv)
        stderr_label = spawn_label
        # The provider-key NAMES the probe proves withheld from a harness child --
        # never the key, which the probe's plugin host does not need. Same validator
        # the session's own injection runs, so a mapping this harness would not
        # honour is refused HERE, before a harness boots on it; the vault itself is
        # opened only for the session. Off-loop: reads config.json.
        try:
            vault_env_names = await asyncio.to_thread(_deepseek_vault_env_names)
        except ValueError as exc:
            try:
                acp_tool_gate.enforce_runtime_routing(
                    self.backend,
                    str(exc),
                    remedy=acp_tool_gate.remediation_for(self.backend),
                )
            except acp_tool_gate.ToolGateUnroutable as gate_exc:
                raise AcpToolGateUnroutable(str(gate_exc)) from None
            # Unreachable while this harness is ENFORCED; kept as the fail-closed
            # floor for the same reason the sites below keep theirs.
            raise AcpToolGateUnroutable(str(exc)) from None
        # The READ-BACK runs HERE, on the argv assembled directly above -- and that
        # argv is the one the session runs: the only step between this point and
        # the real child's wrap is ``apply_pod_bundle_spawn``, which rewrites argv
        # only for a harness in ``ACP_BACKENDS_POD_HOME_REMAP``, and this one is not
        # in that set. So verifying here costs no fidelity, and it is what leaves the
        # shared launch tail exactly as every other backend leaves it: no
        # adapter-driven conditional on the Kiro path (harness-parity H13).
        #
        # The probe gets its OWN THROWAWAY window rather than borrowing the
        # session's. It needs a writable one at all because the managed scratch ROOT
        # is masked for every sandboxed child (``sandbox._CREW_HIDDEN_LEAVES``), and
        # the marker cannot live beside the gate's own code: that leaf is sealed
        # read-only against every harness child so none can plant what a later
        # session loads, which makes it the one place the child cannot create a
        # file. The nonce is in the NAME as well as the contents, so nothing in a
        # reused directory can be mistaken for this probe's marker.
        #
        # ``allocate_scratch`` records the SPAWNING process -- the gateway -- as the
        # window's provisional owner, and the sweep reclaims only
        # owned-and-dead-and-idle directories. The gateway is long-lived, so an
        # abandoned probe window is retained for its whole lifetime and one more per
        # retry: this window is therefore removed EXPLICITLY, in the same
        # ``finally`` as the launcher unlink, rather than left to the sweep.
        try:
            probe_dir = await asyncio.to_thread(
                agent_scratch.allocate_scratch,
                f"{session._session_key or 'session'}-dsh-probe",
            )
        except (OSError, agent_scratch.ScratchBoundaryError) as exc:
            # No window means no marker, so the gate cannot be verified at all.
            # Refused rather than run: without the gate composed this harness
            # executes every in-policy side effect unasked.
            try:
                acp_tool_gate.enforce_runtime_routing(
                    self.backend,
                    "the gate's load marker has nowhere to be written: this "
                    "session got no private scratch directory",
                    remedy=acp_tool_gate.remediation_for(self.backend),
                )
            except acp_tool_gate.ToolGateUnroutable as gate_exc:
                raise AcpToolGateUnroutable(str(gate_exc)) from None
            # Unreachable while this harness is ENFORCED, since the call above
            # raises for every enforced routing. Kept as the fail-closed floor:
            # a routing table that ever stops enforcing this harness must not
            # silently turn an unverifiable gate into an unverified spawn.
            raise AcpToolGateUnroutable(
                "the gate's load marker has nowhere to be written: this session "
                "got no private scratch directory"
            ) from exc
        probe_marker = os.path.join(
            str(probe_dir),
            f"kirocrew_dsh_gate_{self._nonce}.marker.json",
        )
        # Pre-bound so the ``finally`` below can tell "no launcher to unlink" from
        # "the wrap never returned one".
        readback_cleanup: str | None = None
        try:
            readback_argv, readback_cleanup = await wrap_argv_async(
                argv,
                mode=ctx.sandbox_mode,
                strip_python_env=True,
                extra_hidden_dirs=hidden,
                # The probe's own window, re-exposed the way the session's own is.
                # Without it the probe boots under the scratch ROOT mask, its plugin
                # cannot write the marker, and the read-back would report an absent
                # gate for a gate that loaded -- evidence about a different process
                # rather than about this composition.
                extra_private_dirs=(str(probe_dir),),
                # Only the adapter's own re-exposures, exactly as the pi adapter
                # passes. The sealed plugin and the patch are deliberately NOT here:
                # they live in the gate-artifact leaf, which this routing already
                # excludes from the child mask, so the child reads them without a
                # re-exposure -- and asking for one is fatal, because the launcher
                # restores an exposed file by WRITING a copy of it and that leaf is
                # sealed read-only, so the spawn dies with EROFS before the harness
                # starts.
                extra_expose_files=expose,
                _prepare=wrap_argv,
            )
            routing_issue, routing_remedy = await asyncio.to_thread(
                functools.partial(
                    _verify_deepseek_gate,
                    session,
                    self.backend,
                    readback_argv,
                    extension_path,
                    probe_marker,
                    self._nonce,
                    child_scrub_names=vault_env_names,
                )
            )
        finally:
            if readback_cleanup:
                await asyncio.to_thread(launch_mod._unlink_readback_launcher, readback_cleanup)
            # The same removal the sweep makes, run here because the sweep never
            # will: the gateway is this window's provisional owner and is alive.
            # Off-loop, and error-swallowing for the sweep's own reason -- losing a
            # temp directory must not fail the spawn that created it.
            await asyncio.to_thread(shutil.rmtree, probe_dir, ignore_errors=True)
        if routing_issue:
            # Refused before the first prompt, for the reason the pi adapter gives:
            # without the gate composed this harness runs every in-policy side
            # effect unasked, so a session that cannot establish it is a session
            # where none of Crew's tool controls run.
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
            stderr_label=stderr_label,
            extra_hidden_dirs=hidden,
            extra_expose_files=expose,
        )

    def apply_spawn_env(self, env: dict[str, str], *, spawned_binary: str | None = None) -> None:
        """Pin the permission mode and hand the session's child the probe's nonce.

        The mode is pinned rather than left to the ambient value, so a variable
        inherited from the operator's shell cannot select the unconfined mode --
        defence in depth, because the gate plugin is what routes a tool call to
        Crew's gate. The nonce is applied unconditionally: the permission-frame
        tripwire keys on it, so a session that carries no nonce is a session whose
        completed-unasked guard cannot arm. NO marker path: the one load marker is
        the probe's, written into the probe's own window and already judged. The
        session's plugin runs the same gate without writing anything -- it skips the
        write when no path is named -- so whether this session got a private scratch
        window is the hygiene question it is for every other backend, settled in the
        launch tail, not a refusal here.
        """
        env[_ENV_DEEPSEEK_PERMISSION_MODE] = DEEPSEEK_PERMISSION_MODE
        if self._nonce:
            env[_ENV_DSH_GATE_SESSION] = self._nonce

    async def prepare_spawn_env(
        self,
        env: dict[str, str],
        *,
        to_thread: Callable[..., Awaitable[Any]],
    ) -> None:
        """The pinned mode and nonce, then the provider key from Crew's OWN vault.

        The key comes from Crew's vault rather than from a file inside the child's
        tree. This is what lets ``host_auth`` declare no ``adapter_own_leaves`` for
        this harness: both of its credential leaves stay masked for the whole
        process tree, and the key arrives as an environment variable the harness
        resolves ABOVE those files and withholds from every shell it spawns (see
        :data:`_DEEPSEEK_ENV_CHILD_SCRUB_CLASS`).

        Placed before session identity, credential repair and the scrub, which is
        exactly why the validator refuses a name that scrub would strip: a key
        injected here and removed there would leave the operator with a harness that
        cannot reach a model and no error naming why. Off-loop through the guarded
        hop: it reads config.json and the vault while the sandbox launcher is live.
        """
        self.apply_spawn_env(env)
        try:
            deepseek_env, _ = await to_thread(_deepseek_vault_env)
        except ValueError as exc:
            # Fail CLOSED on a mapping this harness would not honour, or a vault
            # secret that is not there. The message names only the operator's own
            # env-var key -- never the vault name and never the value -- so it is
            # safe on the log and in the chat error card. The launcher the wrap wrote
            # is already reclaimed: the guarded hop discards it on ANY exception out
            # of the hop, which is why nothing here discards it again.
            try:
                acp_tool_gate.enforce_runtime_routing(
                    self.backend,
                    str(exc),
                    remedy=acp_tool_gate.remediation_for(self.backend),
                )
            except acp_tool_gate.ToolGateUnroutable as gate_exc:
                raise AcpToolGateUnroutable(str(gate_exc)) from None
            # Unreachable while this harness is ENFORCED; kept as the fail-closed
            # floor for the same reason the launch's own refusals keep theirs.
            raise AcpToolGateUnroutable(str(exc)) from None
        env.update(deepseek_env)
        # The resolver's contract -- clear the PLAINTEXT it returned as soon as
        # nothing needs it from that dict -- is honoured HERE. Copying onto ``env`` is
        # the last read of the resolver's dict, so it is emptied now; ``env`` itself
        # is the dict ``exec`` copies into the child and is a local of the launch,
        # never written to the gateway's ``os.environ`` and never stored on the
        # session, so its plaintext lives exactly as long as that frame. The resolved
        # key NAMES are not kept: the harness withholds the variable from its own
        # shells by name CLASS, not by a list Crew hands it, and nothing on this side
        # reads them later.
        deepseek_env.clear()
