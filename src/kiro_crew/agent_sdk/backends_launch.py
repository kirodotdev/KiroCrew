"""How Kiro Crew launches a harness's process, what confines it, and how it is
recognised later.

The launch table for the harnesses whose own binary serves ACP, the argv0 and npm
package names the PID reclaim matches on, and the two memberships that decide how
the spawned child is confined: Crew's seatbelt or the harness's own sandbox, and a
``$HOME`` relocated onto the pod's tree.

One of the owners of the harness vocabulary in :mod:`kiro_crew.agent_sdk.backends`,
which re-exports every name defined here and is the import path callers use. Like that
module it is a leaf: nothing here imports ``kiro_crew.acp``, ``kiro_crew.config`` or
``kiro_crew.platform`` at module scope. ``scripts/check_harness_parity.py`` lists this file
in ``VOCABULARY_PATHS``, which is what lets its ``ACP_BACKEND*`` names be defined here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Mapping

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKEND_STRANDS,
)

# Backends carrying their OWN internal OS sandbox, which on macOS cannot nest
# inside Kiro Crew's seatbelt (kernel EPERM) — so ``sandbox.wrap_argv`` skips
# Crew's own layer for them. This is the one membership test that fails OPEN:
# claiming it for a harness with no internal sandbox hands isolation to a layer
# that never starts and leaves the agent process unconfined. Only kiro-cli
# qualifies; a Node or Python harness does not, however it is spawned.
#
# KAS is NOT a member, because whether it carries kiro-cli's sandbox depends on
# the route, and a membership here is per harness. Its kiro-cli relay route does,
# and is granted the same delegation by ``ACP_BACKENDS_RELAY_INTERNAL_SANDBOX``
# below; its bundle route runs Node directly, with no kiro-cli in the chain, and
# must keep Crew's layer.
#
# codex-acp is excluded on the same rule: it is a Node adapter, so Crew's own layer
# is the only OS confinement a codex session gets. The Codex sandbox modes the
# adapter can apply are in-process policy, not an OS sandbox that Crew's would
# nest inside.
#
# opencode is excluded, and here the exclusion is load-bearing rather than
# conservative: Crew's own sandbox layer carries the credential mask that is the
# compensating control for this harness's passive reads, so skipping that layer
# would remove the control. It carries no OS sandbox of its own to replace it.
#
# pi is excluded on the same load-bearing ground, and its own documentation says
# so in as many words: pi has no built-in sandbox and runs tools with the
# permissions of its process. Crew's layer is the only one there is.
# deepseek is excluded on that same load-bearing ground, and the thing that makes it
# tempting is exactly what makes it wrong: its composition DOES mount a sandbox of
# its own, and Crew even pins that sandbox's mode. But it confines the harness's own
# tool executors from inside the same process -- it is not an OS sandbox that Crew's
# seatbelt would nest inside, and it does not carry Crew's credential mask. Skipping
# Crew's layer for it would drop the compensating control for its passive reads.
ACP_BACKENDS_INTERNAL_SANDBOX = frozenset({ACP_BACKEND_KIRO})

# Backends whose kiro-cli RELAY route carries kiro-cli's internal sandbox, so a
# spawn on that route takes the delegation ``ACP_BACKENDS_INTERNAL_SANDBOX`` gives
# kiro-cli, and a spawn on any other route of the same harness does not. The
# route is the harness's own positive statement
# (``SpawnPlan.kiro_cli_internal_sandbox``, set only while that sandbox is live on
# the host); this set is what lets that statement count, so a harness cannot waive
# Crew's layer by setting a plan field alone. Read through
# :func:`kiro_crew.agent_sdk.backends.spawn_delegates_to_internal_sandbox`, never
# on its own.
#
# KAS qualifies on its relay route, ``kiro-cli acp --agent-engine v3``: the
# process Crew spawns is kiro-cli, reading the same settings switch as the v2
# engine, and it starts the same internal sandbox before it spawns the KAS
# server, which runs as its child. Measured in the field on macOS (kiro-cli
# 2.34.3-nightly): under Crew's seatbelt the relay printed ``sandbox
# initialization failed: Operation not permitted`` and exited before
# ``initialize``, the nesting refusal the v2 engine gets for the same reason.
# Delegating fixes that refusal; Crew's seatbelt stays on whenever the switch is
# off, exactly as for kiro-cli. Windows is not covered, unlike kiro-cli's own row:
# measured there (kiro-cli 2.35.1), the relay's KAS child runs unconfined with the
# switch on or off, so there is nothing to delegate to and the harness never says
# so. The bundle route is not covered either: it spawns Node with no kiro-cli in
# the chain, and KAS's own sandbox stays off because nothing passes it a
# ``--sandbox`` argument (:mod:`kiro_crew.acp.kas_transport`).
ACP_BACKENDS_RELAY_INTERNAL_SANDBOX = frozenset({ACP_BACKEND_KAS})


# Backends whose pod-spawned child has its ambient ``HOME`` relocated onto the
# pod's own tree, so the MCP OAuth grant artifacts the harness derives from
# ``$HOME`` stay pod-scoped (``acp.client._apply_pod_home_remap``).
#
# Deliberately its OWN set rather than a reuse of
# ``ACP_BACKENDS_INTERNAL_SANDBOX``, even though the membership is identical
# today. The two answer different questions -- "does this harness carry its own
# OS sandbox?" versus "does relocating this harness's HOME move its credential
# store?" -- and conflating them means a harness added to the sandbox set for
# sandbox reasons silently inherits credential-relocation semantics it never
# opted into. That is the capability conflation harness-parity H6 exists to
# prevent, so each membership stays an explicit decision.
#
# Only kiro-cli qualifies: it derives its OAuth artifact directory from ``$HOME``
# with no env override for that subtree alone, which is what makes the remap the
# only reachable lever. A harness that stores credentials elsewhere gains
# nothing from the remap and would only lose its real-home state, so it must not
# be added without checking where it actually reads credentials from.
#
# opencode is excluded because the lever it needs already exists: its credential
# home follows ``XDG_DATA_HOME``, which its auth declaration names, so the
# credential floor re-anchors the declared leaf under the override and no ``$HOME``
# relocation is required to reach it.
#
# pi is excluded for the same reason with a different variable: its whole agent
# directory, credential file included, follows ``PI_CODING_AGENT_DIR``, which its
# auth declaration names.
# deepseek is excluded on the same grounds: its whole home is ``DSH_HOME``, which its
# auth declaration names, so the floor re-anchors the declared leaf under the
# override and a ``$HOME`` relocation reaches nothing the override does not.
# KAS is excluded although Crew spawns it through ``kiro-cli`` too: where KAS keeps
# its MCP OAuth grants is not measured, so it stays out under the rule above.
ACP_BACKENDS_POD_HOME_REMAP = frozenset({ACP_BACKEND_KIRO})


@dataclass(frozen=True)
class SelfServedLaunch:
    """The launch facts of one harness that serves ACP from its own binary.

    ``binary`` is the name searched for on PATH, ``acp_args`` is what follows it on
    the argv, ``bin_env_var`` is the operator override read before the search,
    ``install_command`` is what an absent verdict tells the operator to run, ``label``
    is the harness's display name, and ``protocol_version`` is the handshake dialect.

    ``missing_hint`` is the one field that is prose rather than a value, and it earns
    its place: what an operator would OTHERWISE try to install differs per harness (an
    npm adapter that does not exist; a plugin package that is not the host), and that
    sentence is what stops the wrong install. It is appended to the shared not-found
    message rather than replacing it.

    Frozen because the table is module state every spawn reads: a mutation from one
    session would follow every session after it.
    """

    label: str
    binary: str
    acp_args: tuple
    bin_env_var: str
    install_command: str
    protocol_version: int
    missing_hint: str

    @property
    def spawn_label(self) -> str:
        """What the spawn is logged under: the binary name and its own args."""
        return " ".join((self.binary, *self.acp_args)).strip()


#: The one Copilot CLI release Crew's Copilot defences were measured against. The
#: install hint pins it, so a new install lands on it rather than on whatever is newest,
#: and a session on any other release is refused at the handshake (``acp.
#: client_copilot._refuse_unverified_copilot_version``): several of those defences
#: are lists read off this release's bundle and fail open on a release that adds an
#: entry they do not know.
#: Re-measure against a fresh capture before raising it.
COPILOT_VERIFIED_VERSION = "1.0.100"

#: The one Strands CLI release Crew's Strands gate was measured against: its
#: Python-project ACP server forwards a Strands interrupt as
#: ``session/request_permission`` and refuses a non-empty ``session/new``
#: ``mcpServers`` array, and its worker reads the project and session overrides the
#: gate module depends on. The install hint pins it; another release is named at the
#: handshake (``acp.harness.strands``). Re-measure against a fresh capture before
#: raising it.
STRANDS_VERIFIED_VERSION = "0.2.3"
#: The Python packages the Strands CLI's project worker imports, at the versions the
#: gate was measured on. Installed into the project environment Crew runs Strands in.
STRANDS_PYTHON_REQUIREMENT = "strands-harness==0.3.3"
#: The CLI's own ``engines.node`` floor.
STRANDS_NODE_FLOOR = (22, 0, 0)

#: Harness id -> the fixed facts of LAUNCHING it, for the harnesses whose own binary
#: serves ACP.
#:
#: One row replaces what was a separate site per harness in six places: the binary
#: name, the argv tail, the override variable, the install command, the protocol
#: version and its row in the version table (all ``acp/client.py``), plus the three
#: resolver seams in ``agent_sdk/drivers/acp.py``, the install probe in
#: ``agent_sdk/backend_install.py`` and the display label in
#: ``agent_sdk/tool_gate.py``. Every field is the SAME KIND of thing for all members;
#: a harness whose launch needs a decision rather than a value is not one.
#:
#: It lives in this leaf, beside the other process facts, because H8 keeps
#: harness vocabulary in the files it names and because the install probe reads it without
#: importing ``kiro_crew.acp``.
#:
#: Membership is deliberately narrow. claude-agent-acp, codex-acp and pi-acp are Node
#: adapters resolved through a different ladder -- ``node_modules`` rungs, a vendored
#: entry, and two independently-absent components for pi -- and kiro-cli's argv
#: carries the agent spec, so none of the four is a member and none of their spawn
#: arms reads this table. What a member's arm still owns for itself is its ROUTING:
#: opencode's config read-back, goose's mode seed and deepseek's absence of either
#: are not launch facts and are not here.
ACP_BACKEND_LAUNCH: Mapping[str, SelfServedLaunch] = {
    ACP_BACKEND_OPENCODE: SelfServedLaunch(
        label="OpenCode",
        binary="opencode",
        acp_args=("acp",),
        bin_env_var="OPENCODE_BIN",
        # Pinned to the tested major: the unpinned name installs the latest
        # release, which an operator following this advice after a 2.x release
        # would then have every launch refuse (the version gate in
        # acp/harness/opencode.py names the line it measured).
        install_command="npm i -g opencode-ai@1",
        protocol_version=1,
        missing_hint="No adapter package is needed: this harness serves ACP itself.",
    ),
    ACP_BACKEND_GOOSE: SelfServedLaunch(
        label="goose",
        binary="goose",
        acp_args=("acp",),
        bin_env_var="GOOSE_BIN",
        install_command=(
            "curl -fsSL https://raw.githubusercontent.com/block/goose/main/"
            "download_cli.sh | bash"
        ),
        protocol_version=1,
        missing_hint="No adapter package is needed: this harness serves ACP itself.",
    ),
    ACP_BACKEND_DEEPSEEK: SelfServedLaunch(
        label="DeepSeek Harness",
        binary="dsh",
        acp_args=("--profile", "acp"),
        bin_env_var="DSH_BIN",
        # Exact build the gate routing was measured on; an rc as upstream ships no stable.
        install_command="npm i -g @deepseek-ai/dsh@0.2.0-rc.2",
        protocol_version=1,
        missing_hint=(
            "The ACP plugin package alone does not serve ACP: it is a plugin, and "
            "this binary is the host that boots the profile it lives in."
        ),
    ),
    # ``--no-auto-update`` keeps the child on the binary that was installed: left
    # on, Copilot CLI downloads a newer release into its own package cache and runs
    # THAT on a later launch, which would move a session off the verified range
    # with nothing on the install row saying so. ``--disable-builtin-mcps`` keeps
    # its built-in GitHub MCP servers, which act with the operator's own GitHub
    # credential and are not projected by Crew, out of the session;
    # ``--disallow-temp-dir`` withdraws the access to the system temp directory it
    # otherwise grants itself. Both measured on 1.0.100 to leave a session, its
    # launch MCP mount and its permission requests working.
    ACP_BACKEND_COPILOT: SelfServedLaunch(
        label="GitHub Copilot CLI",
        binary="copilot",
        acp_args=(
            "--acp",
            "--stdio",
            "--no-auto-update",
            "--disable-builtin-mcps",
            "--disallow-temp-dir",
        ),
        bin_env_var="COPILOT_BIN",
        install_command=f"npm i -g @github/copilot@{COPILOT_VERIFIED_VERSION}",
        protocol_version=1,
        missing_hint="No adapter package is needed: this harness serves ACP itself.",
    ),
    # ``--acp-server`` is the CLI's own ACP mode; the project it runs (``--agent``)
    # and the MCP file (``--mcp-config``) are per-launch values the Strands adapter
    # appends, because their paths carry this gateway's data home.
    ACP_BACKEND_STRANDS: SelfServedLaunch(
        label="Strands",
        binary="strands",
        acp_args=("--acp-server",),
        bin_env_var="STRANDS_BIN",
        install_command=f"npm i -g @strands-agents/cli@{STRANDS_VERIFIED_VERSION}",
        protocol_version=1,
        missing_hint=(
            "Install the official Strands CLI; its default --acp-server profile is "
            "not used, Kiro Crew starts it on a Python project it gates."
        ),
    ),
}

#: The harnesses whose whole launch is described by :data:`ACP_BACKEND_LAUNCH`.
#:
#: A driver-internal membership, not a consumer-facing capability: it answers "is this
#: harness's argv a value the table already holds?", which only the spawn path, the
#: install probe and the driver seams ask. Derived from the table's keys rather than
#: written a second time, so the two cannot disagree.
ACP_BACKENDS_SELF_SERVED_ACP: FrozenSet[str] = frozenset(ACP_BACKEND_LAUNCH)

#: The argv0 basename each harness's child process runs as.
#:
#: Read by the PID-file reclaim in :mod:`kiro_crew.session_pid`, which asks one
#: question of a tracked PID it is about to signal: does this PID still name the kind
#: of process the tracking entry described? That is a recycle guard, and it needs a
#: name per harness rather than a capability.
#:
#: Declared HERE rather than in the reclaim, for the reason every per-backend fact is
#: declared in this vocabulary: a harness added to :data:`ACP_BACKENDS_KNOWN` and not to this table
#: is a harness whose orphans the reclaim cannot recognise, and the reclaim's failure
#: mode for an unrecognised orphan is to drop its tracking entry and spare the
#: process — so nothing later can find it. ``test_pid_lifecycle`` ratchets the
#: coverage, so the omission is a red test rather than a leaked process.
#:
#: The three self-served harnesses read their own ``ACP_BACKEND_LAUNCH`` row so the
#: two tables cannot disagree. The rest are spelled out because their launch is
#: bespoke: kiro-cli serves both the kiro and KAS backends (KAS is kiro-cli's relay),
#: and the claude, codex and pi adapters are Node entry scripts whose basenames live
#: with their resolvers in the ACP layer, which this module must not import.
ACP_BACKEND_PROCESS_NAMES: Mapping[str, str] = {
    ACP_BACKEND_KIRO: "kiro-cli",
    ACP_BACKEND_KAS: "kiro-cli",
    ACP_BACKEND_CLAUDE: "claude-agent-acp",
    ACP_BACKEND_CODEX: "codex-acp",
    ACP_BACKEND_PI: "pi-acp",
    **{backend: record.binary for backend, record in sorted(ACP_BACKEND_LAUNCH.items())},
}


#: The npm package that ships each NODE-HOSTED adapter.
#:
#: Three harnesses are entry scripts Crew hands to ``node``; every other harness is a
#: binary that serves ACP itself. Only these three can appear in a command line as
#: ``node <path>``, so only these three need a path to be recognised by.
#:
#: Declared here for the same reason :data:`ACP_BACKEND_PROCESS_NAMES` is: the resolvers
#: live in the ACP layer, which this module must not import, while the reclaim in
#: :mod:`kiro_crew.session_pid` must not import the ACP layer either. One table both can
#: read is what keeps the resolver's spelling and the reclaim's from drifting -- and a
#: drift there is a launch the reclaim cannot recognise, or a path it recognises that
#: Crew never spawns.
#:
#: ``pi-acp`` is unscoped and the other two are scoped: the values are the real published
#: names, not a pattern, because a pattern is what would let an unrelated package satisfy
#: it.
ACP_BACKEND_NODE_ADAPTER_PACKAGES: Mapping[str, str] = {
    ACP_BACKEND_CLAUDE: "@agentclientprotocol/claude-agent-acp",
    ACP_BACKEND_CODEX: "@agentclientprotocol/codex-acp",
    ACP_BACKEND_PI: "pi-acp",
}

#: The entry script inside such a package, as every resolver builds it.
NODE_ADAPTER_ENTRY_SEGMENTS: tuple[str, ...] = ("dist", "index.js")


def node_adapter_entry_relpaths() -> tuple[str, ...]:
    """Every ``<package>/dist/index.js`` tail Crew launches a Node adapter with.

    The IDENTITY of an interpreter-hosted adapter, for a consumer that has a command
    line and must decide whether Crew spawned it. Matching a package DIRECTORY NAME
    against the harness names instead makes "what a process may call itself" an open
    axis: an unrelated npm application at ``node /srv/goose/dist/index.js`` carries a
    directory named after a harness Crew never launches through Node at all. These
    relative paths are a closed set this repository owns, so the axis closes with it.

    Returned as POSIX-separated relative paths, sorted for a stable value.
    """
    return tuple(
        sorted(
            "/".join((package, *NODE_ADAPTER_ENTRY_SEGMENTS))
            for package in ACP_BACKEND_NODE_ADAPTER_PACKAGES.values()
        )
    )


def agent_process_markers() -> tuple[str, ...]:
    """Every harness argv0 basename, sorted and de-duplicated.

    A tuple of substrings for a cmdline match, which is what
    ``platform_compat.process_matches`` takes. Sorted so the value is stable to read
    in a log, de-duplicated because kiro and KAS share ``kiro-cli``.
    """
    return tuple(sorted(set(ACP_BACKEND_PROCESS_NAMES.values())))


def launch_for(backend: str) -> SelfServedLaunch:
    """The launch record for *backend*, raising ``KeyError`` when it has none.

    Raising rather than answering a default is the point: a caller that reaches here
    for a Node adapter or for kiro-cli has taken the wrong arm, and a stand-in record
    would spawn the wrong binary instead of saying so.
    """
    return ACP_BACKEND_LAUNCH[backend]


# These classes keep the facade as their module: it is the path every caller
# imports them from, so a pickled or logged reference names that path.
SelfServedLaunch.__module__ = "kiro_crew.agent_sdk.backends"
