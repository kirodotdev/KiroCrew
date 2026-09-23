"""Every protected name is held for the namespace lifetime, not sampled at spawn.

A mask over a DIRECTORY holds every name inside it, present and future: the
sandbox resolves those names inside the stand-in the launcher created, so a
host-side republish of any of them never appears in that namespace.

A mask over a leaf NAME holds only the object it covers. The name's parent stays
live, so an atomic replace of that name by a host-side writer -- a Dev Fleet
cutover, a config save -- puts a fresh, writable object at a protected name while
the mask hangs off the object that was there at spawn.

These tests enumerate the protected names for every tier from the namespace
plan -- the mask, file and seal lists the launcher is handed -- and pin which of
the two holds each name has. They are static: no namespace, no mount, no
privilege.

POSIX only, and the reason is the legitimate one rather than convenience: the
launcher mask mechanism does not exist on Windows, where the wrap is skipped
entirely, so the invariant asserted here has no subject on that platform. The
skip would be hollow the other way round -- if Windows were itself the behaviour
under test -- but here running on Windows would assert nothing and crash doing
it, because the namespace plan reads ``os.getuid``.
"""

from __future__ import annotations

import os
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

from kiro_crew import sandbox, sandbox_plan
from kiro_crew.config import config_dir

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the namespace plan uses POSIX-only os.getuid; Windows skips the wrap",
)


@pytest.fixture(autouse=True)
def _no_host_ssh_probe(monkeypatch):
    """A namespace plan asks the HOST's ``ssh -V`` for accept-new support.

    The protected-name populations read out of the plan do not depend on that
    answer, and a real ssh spawned from the test process is a host dependency this
    module is not about. Pinned so no binary runs.
    """
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)


TIERS = ("standard", "cc", "strict")

#: Protected names held by an ENCLOSING stand-in mask today. The scratch
#: index stays behind the whole scratch-root mask for the namespace lifetime,
#: including after the gateway atomically republishes the index.
#:
#: A fix that gives a name a durable hold adds it here, and the equality
#: assertion below then locks it: the name can never fall back to leaf-name
#: holding without this constant being edited in the same diff.
HELD_BY_ENCLOSING_MASK: frozenset[str] = frozenset({"scratch/.trees.json"})


def _launcher_sets(tier: str) -> tuple[list[str], list[str]]:
    """The protected names this tier's launcher actually loops over.

    Read from the tier's namespace plan rather than recomputed from the constants,
    so the pin follows what the child is really handed: every masked tree goes into
    both the directory list and the file list and the child classifies by kind, so
    the union is the masked population.

    Returned as two lists because the two kinds of mount hold differently, and
    collapsing them is the mistake this whole file exists to prevent:

    * a MASK is a bind of a stand-in the launcher just created, so a lookup below
      it terminates inside that stand-in and never reaches the real directory;
    * a READ-ONLY SEAL is a bind of a path over ITSELF, which leaves the same
      directory entries in place. It withholds write access; it does not change
      which object a name reaches, so it holds nothing for the names beneath it.
    """
    plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, tier)
    masked = list(dict.fromkeys(plan.sensitive_dirs + plan.sensitive_files))
    return masked, list(dict.fromkeys(plan.readonly))


def _protected(tier: str) -> list[str]:
    masked, readonly = _launcher_sets(tier)
    return list(dict.fromkeys(masked + readonly))


def _enclosing_hold(path: str, stand_in_masks: set[str]) -> str | None:
    """The stand-in mask whose mount holds *path*, or ``None``.

    Only a strict ancestor carrying a STAND-IN mask counts. Such an ancestor
    terminates the sandbox's lookup inside a directory the launcher created
    moments earlier, so nothing a host-side writer does to the real directory
    entry for *path* can reach that namespace -- for the whole namespace
    lifetime, not just at the instant of the spawn.

    A read-only sealed ancestor deliberately does NOT count: see
    :func:`_launcher_sets`.
    """
    parent = os.path.dirname(path.rstrip("/"))
    while parent and parent != "/":
        if parent in stand_in_masks:
            return parent
        parent = os.path.dirname(parent)
    return None


def _split(tier: str) -> tuple[dict[str, str], list[str]]:
    masked, _readonly = _launcher_sets(tier)
    stand_in_masks = set(masked)
    held: dict[str, str] = {}
    leaf_only: list[str] = []
    for path in _protected(tier):
        anchor = _enclosing_hold(path, stand_in_masks)
        if anchor is None:
            leaf_only.append(path)
        else:
            held[path] = anchor
    return held, leaf_only


class TestProtectedNamesAreEnumerable:
    """The population has to be readable from the plan before anything can pin it."""

    @pytest.mark.parametrize("tier", TIERS)
    def test_every_tier_protects_a_nonempty_population(self, tier: str) -> None:
        protected = _protected(tier)
        assert len(protected) > 100, (
            f"{tier} protects only {len(protected)} names; the enumeration is reading "
            "the wrong thing, so every assertion below is vacuous"
        )

    @pytest.mark.parametrize("tier", TIERS)
    def test_every_crew_home_leaf_reaches_the_launcher_payload(self, tier: str) -> None:
        """Two independent derivations of the masked set have to agree.

        The constants say which crew-home leaves are masked; the namespace plan
        says which paths the child will actually mount over. Comparing one
        against the other can fail: a leaf added to the constants that the
        planner stops emitting is a mask silently dropped, which no assertion
        computed from the plan alone could see.
        """
        masked, _readonly = _launcher_sets(tier)
        missing = sorted(
            leaf
            for leaf in sandbox._CREW_HIDDEN_LEAVES
            if not any(path.endswith("/" + leaf) for path in masked)
        )
        assert not missing, (
            f"on {tier} these crew-home leaves are declared masked but reach no path in "
            f"the launcher payload, so nothing is mounted over them: {missing}"
        )

    @pytest.mark.parametrize("tier", TIERS)
    def test_every_protected_name_is_absolute_and_normalized(self, tier: str) -> None:
        """The child mounts by these exact strings.

        A relative entry would resolve against the child's working directory and
        a ``..`` component would resolve somewhere else again, so either one masks
        a path nobody asked to mask and leaves the intended one open.
        """
        bad = sorted(
            path
            for path in _protected(tier)
            if not os.path.isabs(path) or os.path.normpath(path) != path.rstrip("/")
        )
        assert not bad, (
            f"on {tier} these protected paths are not absolute and normalized, so the "
            f"child would mount over a path other than the one intended: {bad}"
        )


class TestEnclosingHoldsNeverRegress:
    """A name held by an enclosing stand-in mask must not fall back to leaf holding.

    This is the durable-hold property itself. A stand-in directory mask covers
    the names inside it for the whole namespace lifetime; a leaf mask covers one
    object at one instant. Moving a name from the first class to the second
    reopens the window whatever else the diff says.

    Asserted in BOTH directions, because the two failures are different bugs and
    one of them is silent. A recorded hold that goes missing is a protection lost.
    A hold the launcher gives that nothing records is a protection nobody is
    watching: its own later loss cannot redden either, so the ratchet goes quiet
    in exactly the direction it exists for.

    The second direction cannot be derived from the first. A set built by
    iterating the recorded names is a subset of them by construction, so
    subtracting it from the recorded names can only ever surface the first
    failure. The unrecorded direction has to be computed from ``held``.
    """

    @pytest.mark.parametrize("tier", TIERS)
    def test_no_recorded_hold_went_missing(self, tier: str) -> None:
        held, _ = _split(tier)
        lost = sorted(
            leaf
            for leaf in HELD_BY_ENCLOSING_MASK
            if not any(path.endswith("/" + leaf) for path in held)
        )
        assert not lost, (
            f"on {tier} these names lost their enclosing stand-in mask and are now held "
            f"only by their own leaf name: {lost}. A host-side atomic replace of such a "
            "name puts a writable object at a protected path for the rest of that "
            "namespace's life."
        )

    @pytest.mark.parametrize("tier", TIERS)
    def test_no_hold_is_unrecorded(self, tier: str) -> None:
        held, _ = _split(tier)
        unrecorded = sorted(
            path
            for path in held
            if not any(path.endswith("/" + leaf) for leaf in HELD_BY_ENCLOSING_MASK)
        )
        assert not unrecorded, (
            f"on {tier} these names are held by an enclosing stand-in mask but are "
            f"recorded nowhere in HELD_BY_ENCLOSING_MASK: {unrecorded}. Record each one "
            "in the same diff that grants it, or its later loss cannot redden."
        )

    @pytest.mark.parametrize("tier", TIERS)
    def test_each_hold_names_a_real_stand_in_ancestor(self, tier: str) -> None:
        held, _ = _split(tier)
        masked, _readonly = _launcher_sets(tier)
        for path, anchor in held.items():
            assert anchor in set(masked), (
                f"{path} is recorded as held by {anchor}, which is not a stand-in mask; "
                "a read-only seal leaves the real directory entries in place and holds "
                "nothing beneath it"
            )
            assert path.startswith(anchor.rstrip("/") + "/")


class TestTheHoldPredicateDiscriminates:
    """The CONTROL: the predicate must be able to answer both ways.

    Without this, a predicate that answered "held" for everything would satisfy
    every assertion above while proving nothing.
    """

    MASK = "/home/u/.kiro/crew/diag"

    def test_a_name_inside_a_masked_directory_is_held(self) -> None:
        assert _enclosing_hold(self.MASK + "/today.jsonl", {self.MASK}) == self.MASK

    def test_a_name_beside_a_masked_directory_is_not_held(self) -> None:
        assert _enclosing_hold("/home/u/.kiro/crew/live_target.json", {self.MASK}) is None

    def test_a_name_is_not_its_own_hold(self) -> None:
        assert _enclosing_hold(self.MASK, {self.MASK}) is None

    def test_a_read_only_sealed_ancestor_is_not_a_hold(self) -> None:
        """The exact reclassification that would make this file lie.

        ``run`` is sealed read-only and ``run/voice-runtime`` sits inside it, so
        counting a sealed ancestor as a hold would report one durably held name
        where there are none. A seal binds a path over ITSELF: same directory
        entries, same rename window, write access withheld and nothing else.
        """
        sealed_ancestor = "/home/u/.kiro/crew/run"
        enclosed = sealed_ancestor + "/voice-runtime"
        # The seal is NOT in the stand-in mask set, so it holds nothing.
        assert _enclosing_hold(enclosed, set()) is None
        # And the enclosed leaf's own mask does not hold the leaf either.
        assert _enclosing_hold(enclosed, {enclosed}) is None


class TestLeafOnlyPopulationIsRecorded:
    """A ratchet on the names held only by their own leaf name.

    Every entry is a name a host-side atomic replace can put a writable object
    at, for the rest of a running namespace's life. The names are recorded so a
    new one cannot be added silently: adding a protected name that only a leaf
    mask holds has to be a deliberate, visible line in :attr:`LEAF_ONLY`.
    """

    #: The leaf-only names, measured. Not a target -- a debt.
    #:
    #: The launcher spells every data-home leaf once per spelling of the crew
    #: home it protects (the two ``$HOME``-joined ``_CREW_HOME_PREFIXES`` plus
    #: the resolved ``config_dir()`` when it is a third place, as the relocated
    #: ``KIROCREW_HOME`` the conftest pins always is), so one new root-level
    #: leaf is three entries in every tier. Seven landed after the first
    #: measurement, all at the data-home root, whose parent no stand-in can
    #: hold, so leaf-only is the only hold available to them:
    #:
    #: * ``credential_redaction.json`` -- the owner's credential-redaction
    #:   switch, on the same read-only floor as ``aws_service_consent.json``;
    #: * ``auth-store-staging`` -- the masked directory the two gateway auth
    #:   stores publish through, so the temp holding a full signing key or
    #:   refresh-chain state is never listable from inside the namespace;
    #: * ``redaction-allow`` -- the reader's allowed link hosts, sealed read-only
    #:   so an agent cannot allow the host it wants to send conversation data to.
    #: * ``panel-dismissals`` -- the operator's subagent-panel dismissals. An
    #:   owner decision about what the panel HIDES, so it sits at the root beside
    #:   ``crew-panels`` and ``crew-teams`` rather than under ``trust/``, which
    #:   stays sandbox read-write for SEL and would leave the record forgeable by
    #:   a runtime-built path. Same hold as those two, and the same reason it can
    #:   only be leaf-only.
    #: * ``config.json`` / ``config.local.json`` -- the owner's settings files,
    #:   sealed read-only so an agent cannot loosen its own settings. Both sit
    #:   at the data-home root, so two leaves are six entries per tier.
    #: * ``vouched-executions`` -- the gateway's restart-surviving vouches for
    #:   member-store admission. A forged file would admit a session to a peer
    #:   member's private memory, so it sits at the root, masked, rather than
    #:   under the sandbox read-write ``trust/``. Leaf-only for the same reason.
    #: * ``pinned-order`` -- the person's pinned-session order, person-only by
    #:   route, masked so an agent cannot rewrite it on disk.
    #:
    #: One more root-level leaf landed since, three more entries per tier:
    #:
    #: * ``registry_trust.json`` -- the operator's grants of ``owner`` trust to a
    #:   hand-configured app registry, on the same read+write floor as
    #:   ``denied_commands.json``, so a writable grant cannot clone a registry the
    #:   agent controls with the machine's git identity.
    #:
    #: Two directories hold what the MCP gateway launches outside the sandbox,
    #: six entries per tier. ``mcp-launch-approvals`` holds the owner's approved
    #: launch fingerprints; ``mcp/resolved`` holds executables substituted for an
    #: approved launch. Each sits beside writable siblings, so no parent stand-in
    #: can hold it.
    #:
    #: One more root-level directory, three entries per tier:
    #:
    #: * ``gateway_id`` -- the id the Remote Crew cycle guard compares, sealed
    #:   read-only so an agent cannot choose whether a chaining loop is detected.
    #:   The spawn mints it before sealing and it never changes after that, so no
    #:   later host-side replace lands on the bound name.
    #: * ``microvm_crews.json`` -- the microvm lane's crew records. Each row
    #:   carries the ``microvm_id`` that ``teardown`` hands to
    #:   ``launcher.terminate``, with no tag to disagree with it and no
    #:   describe-and-confirm step in front of it, so a writable copy lets a
    #:   sandboxed process choose which of the owner's VMs the next delete
    #:   destroys. It sits at the data-home root beside ``cloud.json`` and
    #:   ``cloud_launch_state.json``, which the gateway writes and which the root
    #:   itself must stay writable for, so no enclosing directory mask can hold
    #:   it and leaf-only is the only hold available -- the same reason those two
    #:   are leaf-only.
    #:
    #: One entry per tier for the secret vault at a relocated data home. The
    #: tier lists hold ``.vault`` only at the two ``$HOME``-joined spellings,
    #: so the resolved ``config_dir()`` spelling is a third, leaf-only name,
    #: the same hold ``kas`` gets there.
    #:
    #: ``models`` holds downloaded weights and the speech decoder, sealed read-only
    #: so a sandboxed shell cannot swap the bytes between a loader's digest check
    #: and its reopen. A data-home root leaf, three entries per tier, held by its
    #: own name for the same reason as the root leaves above.
    #:
    #: ``skill-projection`` holds the skill projection's side files: the ownership
    #: sidecars and view ledger a stored ``kirocrew-skill-view-*`` name is mapped
    #: back through to the agent a resumed conversation runs as, plus its leases and
    #: lock. Sealed read-only so a sandboxed shell cannot forge that mapping (the
    #: agents tree it moved out of was already sealed). A data-home root leaf,
    #: three entries per tier, leaf-only for the same reason as the root leaves above.
    #:
    #: One more root-level directory, three entries per tier:
    #:
    #: * ``engines`` -- the KAS engine bundles ``kirocrew kas install`` lays down
    #:   (``acp/kas_bundle.py``), sealed read-only like ``browser-harness`` so a
    #:   sandboxed session cannot rewrite the engine code and Node runtime its
    #:   next session runs. At the data-home root beside writable siblings, so
    #:   leaf-only is the only hold available.
    #:
    #: One more root-level directory, three entries per tier:
    #:
    #: * ``seed-provenance`` -- the settings-seed record, moved out of the root-level
    #:   ``settings_seeds.json`` file into its own directory so the gateway's
    #:   ``atomic_write`` of the record renames INSIDE the sealed name rather than
    #:   over it. That is the move this module asks for: the directory's own inode
    #:   stays the same on a republish. It sits at the data-home root beside
    #:   writable siblings, so it is counted leaf-only like ``skill-projection``.
    #: * ``app-agent-launch`` -- the empty directory kiro-cli runs an app agent's
    #:   launch copy from (``apps.agent_launch.launch_process_cwd``), sealed read-only
    #:   so no sandboxed writer can plant the spec kiro-cli reads there. At the
    #:   data-home root beside writable siblings, so leaf-only is the only hold.
    #:   Three more entries per tier.
    #: * ``browser-harness``, ``browser-harness-ipc``, ``browser-profile`` -- the
    #:   Browser Harness prefix the gateway executes (sealed read-only), the daemon's
    #:   IPC directory and Kiro's Chromium profile (both hidden). They sit at the
    #:   data-home root beside writable siblings, exactly where the retired browser
    #:   CLI's one leaf sat, so leaf-only is the only hold. Three names in, one
    #:   out, both spellings of the data home: four more entries per tier.
    #:
    #: One more root-level directory, three entries per tier:
    #:
    #: * ``member-records`` -- each crewmate's permanent rules, goals and DM
    #:   binding, which the gateway writes on a person's action. Masked at the
    #:   data-home root beside ``crew-teams`` rather than under the sandbox
    #:   read-write ``trust/``, so leaf-only for the same reason.
    #:
    #: One more root-level directory, three entries per tier:
    #:
    #: * ``web-push`` -- the Web Push VAPID key and device subscription secrets
    #:   (``notifications/push_store.py``), masked as a whole directory so the
    #:   ``atomic_write`` temps they publish through are hidden too. At the data-home
    #:   root beside writable siblings, so leaf-only is the only hold.
    #:
    #: One more root-level directory, three entries per tier:
    #:
    #: * ``secret-request-policy`` -- the mediated-secret request directory: the
    #:   owner-signed egress policy ``kirocrew secrets authorize`` writes and its
    #:   authorize lock, masked whole so an agent can neither read the allowlist nor
    #:   plant an entry. Every publication renames INSIDE it, so the directory's own
    #:   inode stays the same on a republish. It sits at the data-home root beside
    #:   writable siblings, so no enclosing directory mask can hold it and leaf-only
    #:   is the only hold available, the same as ``member-records``.
    #:
    #: One more root-level directory, three entries per tier:
    #:
    #: * ``browser-recordings`` -- every chat's browser turn replays
    #:   (``browser_harness.recording``), read only through the owner-only replay
    #:   routes. Masked whole beside ``browser-profile`` at the data-home root, so
    #:   leaf-only is the only hold, like ``work-ledger``.
    #:
    #: One more root-level directory, three entries per tier:
    #:
    #: * ``kas-mcp-launch`` -- the KAS MCP launch snapshots, which hold MCP
    #:   servers' env values (``sandbox_leaves``). Masked whole at the data-home
    #:   root beside writable siblings, so leaf-only is the only hold.
    #:
    #: One entry per tier for the default home's ``run`` under a relocated data
    #: home (``sandbox_workspace.prime_voice_runtime_sandbox_paths``):
    #:
    #: * ``$HOME/.kiro/crew/run`` -- where every gateway leaves its run-dir pointer,
    #:   whatever its data home, and the token mint follows that pointer to a
    #:   launcher it runs outside the sandbox. Sealed read-only so an agent cannot
    #:   plant one. Only the ``$HOME/.kiro/crew`` spelling is added: the relocated
    #:   home's own ``run`` is already counted. It sits at the default home's root
    #:   beside writable siblings, so no enclosing directory mask can hold it and
    #:   leaf-only is the only hold available. The pointer is published by a rename
    #:   INSIDE it, so the directory's own inode stays the same on a republish.
    #:
    #: One more root-level directory, three entries per tier:
    #:
    #: * ``released-sessions`` -- the gateway's pending deletes of released
    #:   engine records. An entry has the gateway delete the record it names, so
    #:   a forged one would delete a conversation. It sits at the root, masked,
    #:   rather than under the sandbox read-write ``trust/``, and is leaf-only for
    #:   the reason ``panel-dismissals`` is.
    #:
    #: One more root-level leaf, three entries per tier:
    #:
    #: * ``shared_crew_preview.json`` -- the owner's Shared Crew switch, a file at
    #:   the data-home root beside writable siblings, so leaf-only is the only
    #:   hold available, as for ``decisions_consent.json``.
    #:
    #: One more root-level directory, three entries per tier:
    #:
    #: * ``config-provenance`` -- the gateway's record of the ``config.json`` and
    #:   ``config.local.json`` objects it published itself, which the launcher's pin
    #:   reads to tell the gateway's own save from a substituted file. Sealed
    #:   read-only so an agent cannot vouch for a file of its choosing. The record is
    #:   republished by a rename INSIDE the directory, so the directory's own inode
    #:   stays the same, as for ``seed-provenance``; at the data-home root beside
    #:   writable siblings, so leaf-only is the only hold.
    #:
    #: One more root-level directory, three entries per tier:
    #:
    #: * ``kirocrew-bin`` -- the one ``kirocrew`` link ``env.augmented_path`` puts
    #:   ahead of ``~/.local/bin`` on every agent PATH, sealed read-only so an agent
    #:   cannot plant the ``kirocrew`` every later lookup runs. The gateway replaces
    #:   the link by a rename INSIDE the directory, so the directory's own inode
    #:   stays the same; at the data-home root beside writable siblings, so
    #:   leaf-only is the only hold, as for ``models``.
    #: One more root-level directory, ``host-exec-grants`` -- the gateway's record of
    #: which apps the owner enabled to run code OUTSIDE the sandbox (a Dev Fleet pod).
    #: The pod gate authorizes on a marker here, so a forged one would start a pod
    #: outside the jail without an owner enable. Sealed read-only so an agent cannot
    #: plant one, and no-follow so a pre-upgrade symlink cannot leave the real path
    #: replaceable. The marker is written by a create INSIDE the directory, so the
    #: directory's own inode stays the same; at the data-home root beside writable
    #: siblings, so leaf-only is the only hold, as for ``kirocrew-bin``.
    #: One more root-level directory, two entries per tier: ``crew-oauth-bridge`` --
    #: the Connection bridge snapshots (``kas_env_shim.BRIDGE_LEAF``), sealed read-only
    #: and no-follow like ``kas-mcp-launch`` so an agent cannot choose what a later
    #: bridge connects as. Each snapshot is written by a rename INSIDE it, so its own
    #: inode stays the same; at the data-home root, so leaf-only is the only hold.
    #: One more root-level file, ``update_next_start.json`` -- the update armed for the
    #: next start, which the boot-time apply installs on. Sealed read-only so an agent
    #: cannot arm one. It sits at the data-home root beside writable siblings and is
    #: published by a rename, so leaf-only is the only hold, as for ``computer_use.json``.
    #: One more root-level directory, three entries per tier: ``update-staging`` -- the
    #: self-update engine's wheel download directory, masked whole so no sandboxed
    #: writer can replace a wheel between its digest check and pip's install. It moved
    #: out of the sandbox read-write ``trust/``; at the data-home root beside writable
    #: siblings, so leaf-only is the only hold, as for ``web-push``.
    #: One more root-level file, ``project_hooks_consent.json`` -- the owner's
    #: per-folder choice to run a checkout's own agent-spec hooks on KAS, sealed
    #: read-only so an agent cannot turn a folder's hooks on for itself; a file at
    #: the data-home root, so leaf-only is the only hold, as for
    #: ``ssh_auth_sock_consent.json``.
    #: One more root-level file, three entries per tier:
    #: ``memory_provider_consent.json`` -- the owner's consent to send memory to a
    #: provider app that keeps it off this machine, sealed read-only so an agent
    #: cannot consent to its own egress; a file at the data-home root, so leaf-only
    #: is the only hold, as for ``decisions_consent.json``.
    #: One more root-level directory, three entries per tier:
    #: ``memory_provider_migrations`` -- memory move jobs and their id ledgers,
    #: sealed read-only so an agent cannot choose what a rollback deletes; a
    #: directory at the data-home root beside writable siblings, so leaf-only is the
    #: only hold, as for ``memory_stores``.
    #: One more root-level directory, three entries per tier:
    #: ``memory_provider_bench`` -- provider benchmark runs, their scratch-store
    #: ledgers and own-memory eval sets, sealed read-only so an agent cannot choose
    #: which store a cleanup resets; a directory at the data-home root, so leaf-only
    #: is the only hold, as for ``memory_provider_migrations``.
    #:
    #: Four app-declared directories, a dozen entries per tier (the data and the
    #: lifecycle's move-aside spelling, at each data-home spelling):
    #:
    #: * ``apps/meetings/data/notes`` and ``apps/spec-builder/data/trust`` -- the
    #:   directories the two built-ins declare in ``agentReadOnlyDirs``
    #:   (``apps.agent_readonly_dirs``), sealed read-only so an agent can neither
    #:   rewrite the owner's notes nor forge or erase a recorded decision. Each app
    #:   writes by a create or rename INSIDE its directory, so the directory's own
    #:   inode stays the same on a republish. The parent is the app's data
    #:   directory, which agents read and write, so an enclosing mask would hide the
    #:   app's data and leaf-only is the only hold, as for ``host-exec-grants``.
    #:
    #: Two more root-level leaves, three entries per tier each (one per data-home path):
    #:
    #: * ``chat_steering_off.json`` -- the steering files each chat switched off,
    #:   sealed read-only so an agent cannot switch off a rule for its own chat. A
    #:   file at the data-home root beside writable siblings, rewritten in place,
    #:   so leaf-only is the only hold, as for ``shared_crew_preview.json``.
    #: * ``mcp_body_fields.json`` -- the MCP message-body fields the owner
    #:   allowed, sealed read-only so an agent cannot allow its own fields past
    #:   the tool_input size ceiling. A file at the data-home root beside
    #:   writable siblings, so leaf-only is the only hold, as for
    #:   ``credential_redaction.json``.
    #:
    #: ``import-staging`` adds three entries per tier. The gateway extracts and
    #: removes temporary trees INSIDE this masked directory, never replacing the
    #: directory itself. Its parent is the data-home root beside writable siblings,
    #: so no enclosing stand-in can hold it: leaf-only by design, like update-staging.
    #:
    #: Six more per tier, one app-declared directory: Notes' ``state/`` under its data
    #: directory (``credentialMasks``), at both data-home spellings and their move-aside
    #: spellings, plus the relocated home. Its parent ``data/`` holds the app's logs, so
    #: it cannot take a stand-in; nothing republishes ``state`` itself (its writers rename
    #: files INSIDE it), and its parents are pinned against rename.
    #:
    #: ``background`` -- the gateway-owned background command records, exit
    #: statuses and logs, sealed read-only so a forged record cannot aim the
    #: gateway's kill -- is one more data-home root leaf, three entries per tier.
    #: The gateway only ever creates names beneath it and never republishes the
    #: leaf itself.
    #:
    #: The record is one line per name, not a count per tier: two changes that each
    #: add a different leaf add different lines and stay green merged, where a count
    #: would be rewritten to the same number by both (testing-conventions.md
    #: § Merge-safe ratchets and counts). A key spells its path with ``<crew>`` for
    #: a crew home, ``~`` for ``$HOME`` and ``<isolated>`` for the suite's
    #: per-test isolation root; the value is how many crew-home spellings the plan
    #: emits for it, and the tiers that hold it. Every entry is exact.
    LEAF_ONLY: dict[str, tuple[int, str]] = {
        "<crew>/.env": (3, "standard cc strict"),
        "<crew>/.kiro_cli_binary_trust.json": (3, "standard cc strict"),
        "<crew>/.vault": (3, "standard cc strict"),
        "<crew>/admission_policy.json": (3, "standard cc strict"),
        "<crew>/agent_model_state.json": (3, "standard cc strict"),
        "<crew>/agent_model_state.json.lock": (3, "standard cc strict"),
        "<crew>/agentcore-inbound": (3, "standard cc strict"),
        "<crew>/app-agent-launch": (3, "standard cc strict"),
        "<crew>/app_admission.json": (3, "standard cc strict"),
        "<crew>/appearance-library": (3, "standard cc strict"),
        "<crew>/apps/.dev-grants.json": (3, "standard cc strict"),
        "<crew>/apps/.md-notebook-data-tmp/state": (3, "standard cc strict"),
        "<crew>/apps/.meetings-data-tmp/notes": (3, "standard cc strict"),
        "<crew>/apps/.spec-builder-data-tmp/trust": (3, "standard cc strict"),
        "<crew>/apps/aws-control/data": (3, "standard cc strict"),
        "<crew>/apps/md-notebook/data/state": (3, "standard cc strict"),
        "<crew>/apps/meetings/data/edits": (3, "standard cc strict"),
        "<crew>/apps/meetings/data/notes": (3, "standard cc strict"),
        "<crew>/apps/spec-builder/data/trust": (3, "standard cc strict"),
        "<crew>/auth-store-staging": (3, "standard cc strict"),
        "<crew>/aws-control-staging": (3, "standard cc strict"),
        "<crew>/aws_service_consent.json": (3, "standard cc strict"),
        "<crew>/background": (3, "standard cc strict"),
        "<crew>/backup": (3, "standard cc strict"),
        "<crew>/browser-cookies.txt": (3, "standard cc strict"),
        "<crew>/browser-engine": (3, "standard cc strict"),
        "<crew>/browser-harness": (3, "standard cc strict"),
        "<crew>/browser-harness-ipc": (3, "standard cc strict"),
        "<crew>/browser-mode-enabled": (3, "standard cc strict"),
        "<crew>/browser-profile": (3, "standard cc strict"),
        "<crew>/browser-recordings": (3, "standard cc strict"),
        "<crew>/chat_steering_off.json": (3, "standard cc strict"),
        "<crew>/cloud.json": (3, "standard cc strict"),
        "<crew>/cloud_launch_state.json": (3, "standard cc strict"),
        "<crew>/computer_use.json": (3, "standard cc strict"),
        "<crew>/config-provenance": (3, "standard cc strict"),
        "<crew>/config.json": (3, "standard cc strict"),
        "<crew>/config.local.json": (3, "standard cc strict"),
        "<crew>/credential_redaction.json": (3, "standard cc strict"),
        "<crew>/crew-log": (3, "standard cc strict"),
        "<crew>/crew-oauth-bridge": (3, "standard cc strict"),
        "<crew>/crew-panels": (3, "standard cc strict"),
        "<crew>/crew-teams": (3, "standard cc strict"),
        "<crew>/cron-history": (3, "standard cc strict"),
        "<crew>/cron-running": (3, "standard cc strict"),
        "<crew>/data.sqlite3": (3, "standard cc strict"),
        "<crew>/data.sqlite3-journal": (3, "standard cc strict"),
        "<crew>/data.sqlite3-shm": (3, "standard cc strict"),
        "<crew>/data.sqlite3-wal": (3, "standard cc strict"),
        "<crew>/decisions": (3, "standard cc strict"),
        "<crew>/decisions_consent.json": (3, "standard cc strict"),
        "<crew>/denied_commands.json": (3, "standard cc strict"),
        "<crew>/diag": (3, "standard cc strict"),
        "<crew>/engines": (3, "standard cc strict"),
        "<crew>/gateway_id": (3, "standard cc strict"),
        "<crew>/host-exec-grants": (3, "standard cc strict"),
        "<crew>/import-staging": (3, "standard cc strict"),
        "<crew>/inbound-spool": (3, "standard cc strict"),
        "<crew>/kas": (3, "standard cc strict"),
        "<crew>/kas-mcp-launch": (3, "standard cc strict"),
        "<crew>/kirocrew-bin": (3, "standard cc strict"),
        "<crew>/ledger": (3, "standard cc strict"),
        "<crew>/ledgers": (3, "standard cc strict"),
        "<crew>/live-target-staging": (3, "standard cc strict"),
        "<crew>/live_target.json": (3, "standard cc strict"),
        "<crew>/mcp-apps": (3, "standard cc strict"),
        "<crew>/mcp-launch-approvals": (3, "standard cc strict"),
        "<crew>/mcp/resolved": (3, "standard cc strict"),
        "<crew>/mcp_body_fields.json": (3, "standard cc strict"),
        "<crew>/md-notebook-staging": (3, "standard cc strict"),
        "<crew>/member-memory-bindings": (3, "standard cc strict"),
        "<crew>/member-records": (3, "standard cc strict"),
        "<crew>/memory_provider_bench": (3, "standard cc strict"),
        "<crew>/memory_provider_consent.json": (3, "standard cc strict"),
        "<crew>/memory_provider_migrations": (3, "standard cc strict"),
        "<crew>/memory_stores": (3, "standard cc strict"),
        "<crew>/microvm_crews.json": (3, "standard cc strict"),
        "<crew>/models": (3, "standard cc strict"),
        "<crew>/oauth_endpoints.json": (3, "standard cc strict"),
        "<crew>/ops_mission_control_policy.json": (3, "standard cc strict"),
        "<crew>/ops_mission_control_secrets.json": (3, "standard cc strict"),
        "<crew>/panel-dismissals": (3, "standard cc strict"),
        "<crew>/panel-templates": (3, "standard cc strict"),
        "<crew>/pi-gate": (3, "standard cc strict"),
        "<crew>/pinned-order": (3, "standard cc strict"),
        "<crew>/playwright-extension-token": (3, "standard cc strict"),
        "<crew>/playwright-storage-state.json": (3, "standard cc strict"),
        "<crew>/policy_cache": (3, "standard cc strict"),
        "<crew>/profiles": (3, "standard cc strict"),
        "<crew>/project_hooks_consent.json": (3, "standard cc strict"),
        "<crew>/quarantined-clones": (3, "standard cc strict"),
        "<crew>/redaction-allow": (3, "standard cc strict"),
        "<crew>/refresh_chains.json": (3, "standard cc strict"),
        "<crew>/registry_trust.json": (3, "standard cc strict"),
        "<crew>/released-sessions": (3, "standard cc strict"),
        "<crew>/routing": (3, "standard cc strict"),
        "<crew>/run": (1, "standard cc strict"),
        "<crew>/run/voice-runtime": (3, "standard cc strict"),
        "<crew>/scratch": (3, "standard cc strict"),
        "<crew>/secret-request-policy": (3, "standard cc strict"),
        "<crew>/security_policy.json": (3, "standard cc strict"),
        "<crew>/seed-provenance": (3, "standard cc strict"),
        "<crew>/settings_seeds.json": (3, "standard cc strict"),
        "<crew>/shared_crew_preview.json": (3, "standard cc strict"),
        "<crew>/skill-projection": (3, "standard cc strict"),
        "<crew>/ssh_auth_sock_consent.json": (3, "standard cc strict"),
        "<crew>/subagents": (3, "standard cc strict"),
        "<crew>/tag-grants": (3, "standard cc strict"),
        "<crew>/tasks": (3, "standard cc strict"),
        "<crew>/template-mcp-additions": (3, "standard cc strict"),
        "<crew>/token_signing.key": (3, "standard cc strict"),
        "<crew>/update-staging": (3, "standard cc strict"),
        "<crew>/update_next_start.json": (3, "standard cc strict"),
        "<crew>/vouched-executions": (3, "standard cc strict"),
        "<crew>/web-push": (3, "standard cc strict"),
        "<crew>/webhooks": (3, "standard cc strict"),
        "<crew>/whatsapp": (3, "standard cc strict"),
        "<crew>/work": (3, "standard cc strict"),
        "<crew>/work-ledger": (3, "standard cc strict"),
        "<crew>/workflow_library": (3, "standard cc strict"),
        "<crew>/workspace/md-notebook/pat": (3, "standard cc strict"),
        "<crew>/workspace/md-notebook/settings.json": (3, "standard cc strict"),
        "<crew>/workspace/md-notebook/vaults.json": (3, "standard cc strict"),
        "<isolated>/kiro-agents": (1, "standard cc strict"),
        "<isolated>/kiro-home/.kiro/crew/run": (1, "standard cc strict"),
        "~/.aws": (1, "cc strict"),
        "~/.azure": (1, "standard cc strict"),
        "~/.config/gcloud": (1, "standard cc strict"),
        "~/.config/gh": (1, "strict"),
        "~/.docker": (1, "standard cc strict"),
        "~/.git-credentials": (1, "cc strict"),
        "~/.gnupg": (1, "standard cc strict"),
        "~/.gpg": (1, "standard cc strict"),
        "~/.kiro/crew-auth-staging": (1, "standard cc strict"),
        "~/.kube": (1, "cc strict"),
        "~/.midway": (1, "cc strict"),
        "~/.netrc": (1, "cc strict"),
        "~/.npmrc": (1, "cc strict"),
        "~/.pypirc": (1, "cc strict"),
    }

    def test_leaf_only_names_match_the_record(self) -> None:
        observed = {tier: _leaf_only_names(_split(tier)[1]) for tier in TIERS}
        drift = _leaf_only_drift(observed, self.LEAF_ONLY)
        assert not drift, _leaf_only_growth_message(drift)

    def test_the_drift_names_the_line_to_write(self) -> None:
        recorded = {"<crew>/a": (3, "standard cc strict"), "<crew>/b": (3, "strict")}
        observed = {
            "standard": {"<crew>/a": 3, "~/.new": 1},
            "cc": {"<crew>/a": 3},
            "strict": {"<crew>/a": 3},
        }
        assert _leaf_only_drift(observed, recorded) == [
            '        "<crew>/b": (0, ""),  # was (3, "strict")',
            '        "~/.new": (1, "standard"),  # was (0, "")',
        ]
        assert _leaf_only_drift({t: {"<crew>/a": 3} for t in TIERS}, recorded) == [
            '        "<crew>/b": (0, ""),  # was (3, "strict")'
        ]

    def test_the_growth_message_states_the_root_leaf_decision(self) -> None:
        """The failure text is the next author's only guidance, so it is pinned.

        It must name both routes: an enclosing mask where the parent can take a
        stand-in, and the recorded leaf-only decision for a data-home root leaf,
        whose parent no stand-in can hold.
        """
        line = '        "<crew>/x": (3, "standard"),  # was (0, "")'
        message = _leaf_only_growth_message([line])
        assert line in message
        assert "enclosing directory mask" in message
        assert "no stand-in can hold" in message
        assert "leaf-only by design" in message
        assert "credential_redaction.json" in message
        assert "LEAF_ONLY" in message


def _leaf_only_names(paths: list[str]) -> dict[str, int]:
    """*paths* keyed the way :attr:`TestLeafOnlyPopulationIsRecorded.LEAF_ONLY` is.

    The crew homes and the isolation root are matched before ``$HOME``: under
    some runners the isolation root sits below ``$HOME``, and its homes must not
    read as the real ones.
    """
    crew = str(config_dir())
    home = str(Path.home())
    # A runner whose temp dir sits behind a symlink (macOS ``/var``) may hand the
    # plan either spelling of the isolation root.
    roots = dict.fromkeys((str(Path(crew).parent), os.path.realpath(Path(crew).parent)))
    isolated = re.compile("(?:" + "|".join(map(re.escape, roots)) + r")/\d+-")
    prefixes = tuple(
        (root, "<crew>")
        for root in dict.fromkeys(
            (crew, os.path.realpath(crew), home + "/.kiro/crew", home + "/.kirocrew")
        )
    )
    names: Counter[str] = Counter()
    for path in paths:
        key = next(
            (
                token + path[len(root) :]
                for root, token in prefixes
                if path == root or path.startswith(root + "/")
            ),
            None,
        )
        if key is None:
            key = isolated.sub("<isolated>/", path, count=1)
        if key == path and (path == home or path.startswith(home + "/")):
            key = "~" + path[len(home) :]
        names[key] += 1
    return dict(names)


def _row(value: tuple[int, str]) -> str:
    return f'({value[0]}, "{value[1]}")'


def _leaf_only_drift(
    observed: dict[str, dict[str, int]], recorded: dict[str, tuple[int, str]]
) -> list[str]:
    """The ``LEAF_ONLY`` line to write for every name whose spellings or tiers moved.

    A name the tiers spell a different number of times measures as ``-1``, which
    no recorded line can match.
    """
    measured: dict[str, tuple[int, str]] = {}
    for name in sorted({n for names in observed.values() for n in names}):
        counts = {observed[tier][name] for tier in TIERS if name in observed[tier]}
        tiers = " ".join(tier for tier in TIERS if name in observed[tier])
        measured[name] = (counts.pop() if len(counts) == 1 else -1, tiers)
    return [
        f'        "{name}": {_row(measured.get(name, (0, "")))},'
        f"  # was {_row(recorded.get(name, (0, '')))}"
        for name in sorted(set(measured) | set(recorded))
        if measured.get(name) != recorded.get(name)
    ]


def _leaf_only_growth_message(drift: list[str]) -> str:
    """The ratchet's failure text for leaf-only names that moved."""
    return (
        "The protected names held by their own leaf name only no longer match "
        "LEAF_ONLY. Each new one is a name a host-side republish can leave "
        "writable inside a live namespace. If its parent directory can take a "
        "stand-in, hold it with an enclosing directory mask instead. A leaf at the "
        "data-home root cannot: no stand-in can hold the root, so such a leaf is "
        "leaf-only by design, as credential_redaction.json and the other root "
        "leaves recorded in TestLeafOnlyPopulationIsRecorded are. For one of those, "
        "write its LEAF_ONLY line and record its reason beside it in the same diff; "
        "for a name that is gone (a spelling count of 0), delete its line:\n" + "\n".join(drift)
    )
