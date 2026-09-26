"""Every protected name is held for the namespace lifetime, not sampled at spawn.

A mask over a DIRECTORY holds every name inside it, present and future: the
sandbox resolves those names inside the stand-in the launcher created, so a
host-side republish of any of them never appears in that namespace.

A mask over a leaf NAME holds only the object it covers. The name's parent stays
live, so an atomic replace of that name by a host-side writer -- a Dev Fleet
cutover, a config save -- puts a fresh, writable object at a protected name while
the mask hangs off the object that was there at spawn.

These tests enumerate the protected names FROM SOURCE for every tier and pin
which of the two holds each name has. They are static: no namespace, no mount,
no privilege.

POSIX only, and the reason is the legitimate one rather than convenience: the
launcher mask mechanism does not exist on Windows, where the wrap is skipped
entirely, so the invariant asserted here has no subject on that platform. The
skip would be hollow the other way round -- if Windows were itself the behaviour
under test -- but here running on Windows would assert nothing and crash doing
it, because the launcher builder reads ``os.getuid``.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from kiro_crew import sandbox

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="_build_launcher_script uses POSIX-only os.getuid; Windows skips the wrap",
)

TIERS = ("standard", "cc", "strict")

#: Protected names held by an ENCLOSING stand-in mask today. Measured, not aspired
#: to: it is EMPTY, because nothing in the launcher holds a protected
#: name for the namespace lifetime. Every mask is placed once, at spawn.
#:
#: A fix that gives a name a durable hold adds it here, and the equality
#: assertion below then locks it: the name can never fall back to leaf-name
#: holding without this constant being edited in the same diff.
HELD_BY_ENCLOSING_MASK: frozenset[str] = frozenset()


def _launcher_sets(tier: str) -> tuple[list[str], list[str]]:
    """The protected names this tier's launcher actually loops over.

    Read back out of the generated script rather than recomputed from the
    constants, so the pin follows what the child is really handed: every entry
    goes into both the directory list and the file list and the child classifies
    by kind, so the union is the masked population.

    Returned as two lists because the two kinds of mount hold differently, and
    collapsing them is the mistake this whole file exists to prevent:

    * a MASK is a bind of a stand-in the launcher just created, so a lookup below
      it terminates inside that stand-in and never reaches the real directory;
    * a READ-ONLY SEAL is a bind of a path over ITSELF, which leaves the same
      directory entries in place. It withholds write access; it does not change
      which object a name reaches, so it holds nothing for the names beneath it.
    """
    script = sandbox._build_launcher_script(tier)
    found: dict[str, list[str]] = {}
    for line in script.splitlines():
        for key in ("SENSITIVE_DIRS", "SENSITIVE_FILES", "READONLY_DIRS"):
            if line.startswith(key + " = "):
                found[key] = json.loads(line.split(" = ", 1)[1])
    missing = {"SENSITIVE_DIRS", "SENSITIVE_FILES", "READONLY_DIRS"} - set(found)
    assert not missing, f"launcher script does not emit {sorted(missing)}"
    masked = list(dict.fromkeys(found["SENSITIVE_DIRS"] + found["SENSITIVE_FILES"]))
    return masked, list(dict.fromkeys(found["READONLY_DIRS"]))


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
    """The population has to be readable from source before anything can pin it."""

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

        The constants say which crew-home leaves are masked; the generated script
        says which paths the child will actually mount over. Comparing one
        against the other can fail: a leaf added to the constants that the
        builder stops emitting is a mask silently dropped, which no assertion
        computed from the script alone could see.
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
    at, for the rest of a running namespace's life. The count is recorded so a
    new one cannot be added silently: adding a protected name that only a leaf
    mask holds has to be a deliberate, visible edit to this number.
    """

    #: Measured per tier. Not a target -- a debt. Today it is the WHOLE
    #: population: nothing is durably held.
    #:
    #: The launcher spells every data-home leaf once per spelling of the crew
    #: home it protects (the two ``$HOME``-joined ``_CREW_HOME_PREFIXES`` plus
    #: the resolved ``config_dir()`` when it is a third place, as the relocated
    #: ``KIROCREW_HOME`` the conftest pins always is), so one new root-level
    #: leaf is three entries in every tier. Three landed after the first
    #: measurement, all at the data-home root, whose parent no stand-in can
    #: hold, so leaf-only is the only hold available to them:
    #:
    #: * ``credential_redaction.json`` -- the owner's credential-redaction
    #:   switch, on the same read-only floor as ``file_delivery_consent.json``;
    #: * ``auth-store-staging`` -- the masked directory the two gateway auth
    #:   stores publish through, so the temp holding a full signing key or
    #:   refresh-chain state is never listable from inside the namespace;
    #: * ``redaction-allow`` -- the reader's allowed link hosts, sealed read-only
    #:   so an agent cannot allow the host it wants to send conversation data to.
    EXPECTED: dict[str, int] = {"standard": 244, "cc": 251, "strict": 252}

    @pytest.mark.parametrize("tier", TIERS)
    def test_leaf_only_count_has_not_grown(self, tier: str) -> None:
        _, leaf_only = _split(tier)
        expected = self.EXPECTED[tier]
        assert len(leaf_only) <= expected, (
            f"{tier} now holds {len(leaf_only)} protected names by their own leaf name "
            f"only, up from {expected}. Each new one is a name a host-side republish can "
            "leave writable inside a live namespace; hold it with an enclosing directory "
            "mask instead of adding it here."
        )
