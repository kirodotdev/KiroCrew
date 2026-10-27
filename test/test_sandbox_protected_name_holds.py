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
from kiro_crew.apps import agent_readonly_dirs, credential_masks
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


@pytest.fixture(autouse=True)
def _plain_gateway_checkout(_floor_monkeypatch):
    """Pin the gateway checkout's git layout to one plain checkout.

    The live layout follows whatever checkout runs the tests (a plain clone in CI, a
    linked worktree on a developer host), so the population would differ by host.
    """
    from kiro_crew import sandbox_git_control as gc

    gitdir = "/srv/checkout/.git"
    layout = gc.GitControlLayout(
        pins=("/srv/checkout", gitdir),
        readonly=tuple(
            f"{gitdir}/{leaf}"
            for leaf in ("hooks", "info", "config", "config.worktree", "commondir")
        ),
        guards=(),
    )
    _floor_monkeypatch.setattr(gc, "resolved_git_control_layout", lambda: layout)


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


class TestLeafOnlyNamesHaveNoBetterHold:
    """Every name held only by its own leaf mask is one no enclosing stand-in can hold.

    A leaf-only name is one a host-side atomic replace can put a writable object
    at for the rest of a running namespace's life. That is the only hold available
    where the parent directory has to stay writable, so it is leaf-only by design:

    * a name directly at a root -- a crew-home spelling, ``$HOME`` or the suite's
      isolation root -- beside writable siblings no stand-in may hide;
    * an app-declared directory (``agentReadOnlyDirs`` / ``credentialMasks``,
      read from :mod:`kiro_crew.apps`), whose parent is the app's data directory
      that agents read and write.

    Any other leaf-only name sits below a directory that might take a stand-in, so
    it is a recorded decision in :attr:`NESTED_LEAF_ONLY`.
    """

    #: Review list: nested leaf-only names, each one decided by hand because its
    #: parent is a directory an enclosing mask could in principle hold. A new
    #: nested leaf-only name fails until someone confirms no stand-in can hold its
    #: parent and adds it here; a name that stops being leaf-only fails until its
    #: line is deleted.
    NESTED_LEAF_ONLY: frozenset[str] = frozenset(
        {
            # ``apps/`` holds every installed app, which agents read and write.
            "<crew>/apps/.dev-grants.json",
            # Gateway-written app data beside the app's agent-writable logs.
            "<crew>/apps/aws-control/data",
            "<crew>/apps/meetings/data/edits",
            # ``mcp/`` holds writable siblings beside the resolved launch executables.
            "<crew>/mcp/resolved",
            # ``run/`` is sealed read-only, and a seal holds nothing beneath it.
            "<crew>/run/voice-runtime",
            # The Notes builtin's retired state names under its writable workspace.
            "<crew>/workspace/md-notebook/pat",
            "<crew>/workspace/md-notebook/settings.json",
            "<crew>/workspace/md-notebook/vaults.json",
            # The default home's ``run`` under the suite's relocated ``$HOME``.
            "<isolated>/kiro-home/.kiro/crew/run",
            # ``~/.config`` and ``~/.kiro`` hold the user's other tools' state.
            "~/.config/gcloud",
            "~/.config/gh",
            "~/.kiro/crew-auth-staging",
            # The gateway checkout's git control files, as ``_plain_gateway_checkout``
            # pins them: they sit in the git directory beside the index, objects and
            # refs that in-sandbox commits write, so no enclosing mask can hold them.
            "/srv/checkout/.git/commondir",
            "/srv/checkout/.git/config",
            "/srv/checkout/.git/config.worktree",
            "/srv/checkout/.git/hooks",
            "/srv/checkout/.git/info",
        }
    )

    #: Review list for REMOVALS: every name below must stay protected from the tier
    #: named on its line upward (the stricter tiers follow from
    #: :meth:`test_a_stricter_tier_protects_every_name_a_looser_one_does`). It guards
    #: the governance and credential files an agent must never write, such as
    #: ``vouched-executions`` and ``config.json``. A newly protected name needs no line
    #: here; dropping a protection fails until its line is deleted on purpose.
    MUST_STAY_PROTECTED: dict[str, str] = {
        "<crew>/.env": "standard",
        "<crew>/.kiro_cli_binary_trust.json": "standard",
        "<crew>/.vault": "standard",
        "<crew>/admission_policy.json": "standard",
        "<crew>/agent_model_state.json": "standard",
        "<crew>/agent_model_state.json.lock": "standard",
        "<crew>/agentcore-inbound": "standard",
        "<crew>/app-agent-launch": "standard",
        "<crew>/app_admission.json": "standard",
        "<crew>/appearance-library": "standard",
        "<crew>/apps/.dev-grants.json": "standard",
        "<crew>/apps/.md-notebook-data-tmp/state": "standard",
        "<crew>/apps/.meetings-data-tmp/notes": "standard",
        "<crew>/apps/.spec-builder-data-tmp/trust": "standard",
        "<crew>/apps/aws-control/data": "standard",
        "<crew>/apps/md-notebook/data/state": "standard",
        "<crew>/apps/meetings/data/edits": "standard",
        "<crew>/apps/meetings/data/notes": "standard",
        "<crew>/apps/spec-builder/data/trust": "standard",
        "<crew>/auth-store-staging": "standard",
        "<crew>/aws-control-staging": "standard",
        "<crew>/aws_service_consent.json": "standard",
        "<crew>/backup": "standard",
        "<crew>/browser-cookies.txt": "standard",
        "<crew>/browser-engine": "standard",
        "<crew>/browser-harness": "standard",
        "<crew>/browser-harness-ipc": "standard",
        "<crew>/browser-mode-enabled": "standard",
        "<crew>/browser-profile": "standard",
        "<crew>/browser-recordings": "standard",
        "<crew>/chat_steering_off.json": "standard",
        "<crew>/cloud.json": "standard",
        "<crew>/cloud_launch_state.json": "standard",
        "<crew>/computer_use.json": "standard",
        "<crew>/config-provenance": "standard",
        "<crew>/config.json": "standard",
        "<crew>/config.local.json": "standard",
        "<crew>/credential_redaction.json": "standard",
        "<crew>/crew-log": "standard",
        "<crew>/crew-oauth-bridge": "standard",
        "<crew>/crew-panels": "standard",
        "<crew>/crew-teams": "standard",
        "<crew>/cron-history": "standard",
        "<crew>/cron-running": "standard",
        "<crew>/data.sqlite3": "standard",
        "<crew>/data.sqlite3-journal": "standard",
        "<crew>/data.sqlite3-shm": "standard",
        "<crew>/data.sqlite3-wal": "standard",
        "<crew>/decisions": "standard",
        "<crew>/decisions_consent.json": "standard",
        "<crew>/denied_commands.json": "standard",
        "<crew>/diag": "standard",
        "<crew>/engines": "standard",
        "<crew>/extras": "standard",
        "<crew>/gateway_id": "standard",
        "<crew>/host-exec-grants": "standard",
        "<crew>/import-staging": "standard",
        "<crew>/inbound-spool": "standard",
        "<crew>/kas": "standard",
        "<crew>/kas-mcp-launch": "standard",
        "<crew>/kirocrew-bin": "standard",
        "<crew>/ledger": "standard",
        "<crew>/ledgers": "standard",
        "<crew>/live-target-staging": "standard",
        "<crew>/live_target.json": "standard",
        "<crew>/market-registries": "standard",
        "<crew>/mcp-apps": "standard",
        "<crew>/mcp-launch-approvals": "standard",
        "<crew>/mcp/resolved": "standard",
        "<crew>/mcp_body_fields.json": "standard",
        "<crew>/md-notebook-staging": "standard",
        "<crew>/member-memory-bindings": "standard",
        "<crew>/member-records": "standard",
        "<crew>/memory_provider_bench": "standard",
        "<crew>/memory_provider_consent.json": "standard",
        "<crew>/memory_provider_migrations": "standard",
        "<crew>/memory_stores": "standard",
        "<crew>/microvm_crews.json": "standard",
        "<crew>/models": "standard",
        "<crew>/oauth_endpoints.json": "standard",
        "<crew>/ops_mission_control_policy.json": "standard",
        "<crew>/ops_mission_control_secrets.json": "standard",
        "<crew>/panel-dismissals": "standard",
        "<crew>/panel-templates": "standard",
        "<crew>/pi-gate": "standard",
        "<crew>/pinned-order": "standard",
        "<crew>/playwright-extension-token": "standard",
        "<crew>/playwright-storage-state.json": "standard",
        "<crew>/policy_cache": "standard",
        "<crew>/profiles": "standard",
        "<crew>/project_hooks_consent.json": "standard",
        "<crew>/quarantined-clones": "standard",
        "<crew>/redaction-allow": "standard",
        "<crew>/refresh_chains.json": "standard",
        "<crew>/registry_trust.json": "standard",
        "<crew>/released-sessions": "standard",
        "<crew>/routing": "standard",
        "<crew>/run": "standard",
        "<crew>/run/voice-runtime": "standard",
        "<crew>/scratch": "standard",
        "<crew>/secret-request-policy": "standard",
        "<crew>/security_policy.json": "standard",
        "<crew>/seed-provenance": "standard",
        "<crew>/settings_seeds.json": "standard",
        "<crew>/shared_crew_preview.json": "standard",
        "<crew>/skill-projection": "standard",
        "<crew>/ssh_auth_sock_consent.json": "standard",
        "<crew>/subagents": "standard",
        "<crew>/tag-grants": "standard",
        "<crew>/tasks": "standard",
        "<crew>/template-mcp-additions": "standard",
        "<crew>/token_signing.key": "standard",
        "<crew>/update-staging": "standard",
        "<crew>/update_next_start.json": "standard",
        "<crew>/vouched-executions": "standard",
        "<crew>/web-push": "standard",
        "<crew>/webhooks": "standard",
        "<crew>/whatsapp": "standard",
        "<crew>/work": "standard",
        "<crew>/work-ledger": "standard",
        "<crew>/workflow_library": "standard",
        "<crew>/workspace/md-notebook/pat": "standard",
        "<crew>/workspace/md-notebook/settings.json": "standard",
        "<crew>/workspace/md-notebook/vaults.json": "standard",
        "<isolated>/kiro-agents": "standard",
        "<isolated>/kiro-home/.kiro/crew/run": "standard",
        "~/.aws": "cc",
        "~/.azure": "standard",
        "~/.config/gcloud": "standard",
        "~/.config/gh": "strict",
        "~/.docker": "standard",
        "~/.git-credentials": "cc",
        "~/.gnupg": "standard",
        "~/.gpg": "standard",
        "~/.kiro/crew-auth-staging": "standard",
        "~/.kube": "cc",
        "~/.midway": "cc",
        "~/.netrc": "cc",
        "~/.npmrc": "cc",
        "~/.pypirc": "cc",
    }

    #: Crew-home names the plan spells at fewer homes than the rest: the
    #: relocated home's ``run``, whose default-home spelling is the
    #: ``<isolated>`` line above.
    FEWER_SPELLINGS: frozenset[str] = frozenset({"<crew>/run"})

    @staticmethod
    def _observed() -> dict[str, dict[str, int]]:
        return {tier: _leaf_only_names(_split(tier)[1]) for tier in TIERS}

    @pytest.mark.parametrize("tier", TIERS)
    def test_every_leaf_only_name_has_no_better_hold(self, tier: str) -> None:
        names = self._observed()[tier]
        assert names, f"{tier} has no leaf-only names; the enumeration is reading nothing"
        unexplained = sorted(
            name
            for name in names
            if not _leaf_only_by_design(name) and name not in self.NESTED_LEAF_ONLY
        )
        assert not unexplained, (
            f"on {tier} these protected names are held only by their own leaf name, "
            "below a parent that is not a root: a host-side republish can leave each "
            "one writable inside a live namespace. Hold it with an enclosing "
            "directory mask if its parent can take a stand-in; if it cannot, add it "
            f"to NESTED_LEAF_ONLY with the reason: {unexplained}"
        )

    def test_every_recorded_nested_name_is_still_leaf_only(self) -> None:
        observed = set().union(*self._observed().values())
        stale = sorted(self.NESTED_LEAF_ONLY - observed)
        assert not stale, (
            f"these NESTED_LEAF_ONLY names are no longer leaf-only in any tier; "
            f"delete their lines: {stale}"
        )

    def test_a_name_is_spelled_alike_in_every_tier(self) -> None:
        observed = self._observed()
        mismatched = sorted(
            name
            for name in set().union(*observed.values())
            if len({observed[tier][name] for tier in TIERS if name in observed[tier]}) > 1
        )
        assert not mismatched, (
            f"these leaf-only names are spelled a different number of times in "
            f"different tiers, so one tier leaves a spelling unprotected: {mismatched}"
        )

    def test_a_stricter_tier_protects_every_name_a_looser_one_does(self) -> None:
        protected = {tier: set(_leaf_only_names(_protected(tier))) for tier in TIERS}
        dropped = {
            f"{looser}->{stricter}": sorted(protected[looser] - protected[stricter])
            for looser, stricter in zip(TIERS, TIERS[1:])
            if protected[looser] - protected[stricter]
        }
        assert not dropped, f"a stricter tier leaves these names unprotected: {dropped}"

    def test_no_recorded_name_loses_its_protection(self) -> None:
        protected = {tier: set(_leaf_only_names(_protected(tier))) for tier in TIERS}
        lost = sorted(
            f"{name} ({tier})"
            for name, lowest in self.MUST_STAY_PROTECTED.items()
            for tier in TIERS[TIERS.index(lowest) :]
            if name not in protected[tier]
        )
        assert not lost, (
            "these names are no longer protected in a tier that must protect them; an "
            "agent in that tier can now write them. Restore the protection, or delete "
            f"the line from MUST_STAY_PROTECTED if dropping it is intended: {lost}"
        )

    @pytest.mark.parametrize("tier", TIERS)
    def test_a_crew_home_name_is_spelled_at_every_crew_home(self, tier: str) -> None:
        crew = {
            name: count
            for name, count in self._observed()[tier].items()
            if name.startswith("<crew>/") and name not in self.FEWER_SPELLINGS
        }
        # The crew homes a sandbox must hide, read from the home layout rather than
        # from the plan, so a plan that drops a whole home's masks fails: the active
        # home, the default ``~/.kiro/crew`` and the legacy ``~/.kirocrew``.
        home = str(Path.home())
        homes = len(dict.fromkeys((str(config_dir()), home + "/.kiro/crew", home + "/.kirocrew")))
        assert crew, f"{tier} protects no crew-home name; the enumeration is reading nothing"
        short = sorted(name for name, count in crew.items() if count != homes)
        assert not short, (
            f"on {tier} these crew-home names are not spelled at all {homes} crew homes, "
            f"so a crew home is left unprotected: {short}"
        )


def _leaf_only_by_design(name: str) -> bool:
    """Whether *name* (keyed as :func:`_leaf_only_names` keys it) has no parent to hold.

    True for a name directly under a root token and for an app-declared directory.
    """
    if os.path.dirname(name) in ("<crew>", "~", "<isolated>"):
        return True
    declared = {
        *agent_readonly_dirs.declared_crew_leaves(),
        *credential_masks.declared_crew_leaves(),
    }
    return name.startswith("<crew>/") and name[len("<crew>/") :] in declared


class TestTheByDesignPredicateDiscriminates:
    """The CONTROL: a nested name outside the app declarations is not by design."""

    def test_root_leaves_are_by_design(self) -> None:
        assert _leaf_only_by_design("<crew>/config.json")
        assert _leaf_only_by_design("~/.aws")

    def test_a_declared_app_directory_is_by_design(self) -> None:
        leaf = min(agent_readonly_dirs.declared_crew_leaves())
        assert _leaf_only_by_design("<crew>/" + leaf)

    def test_a_nested_name_is_not(self) -> None:
        assert not _leaf_only_by_design("<crew>/mcp/resolved")
        assert not _leaf_only_by_design("~/.config/gh")


def _leaf_only_names(paths: list[str]) -> dict[str, int]:
    """*paths* keyed by spelling, with a count of spellings per key.

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
