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
no privilege, so they run wherever the suite runs.
"""

from __future__ import annotations

import json
import os

import pytest

from kiro_crew import sandbox

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
    def test_the_two_holds_partition_the_population(self, tier: str) -> None:
        held, leaf_only = _split(tier)
        protected = _protected(tier)
        assert len(held) + len(leaf_only) == len(protected)
        assert not (set(held) & set(leaf_only))


class TestEnclosingHoldsNeverRegress:
    """A name held by an enclosing stand-in mask must not fall back to leaf holding.

    This is the durable-hold property itself. A stand-in directory mask covers
    the names inside it for the whole namespace lifetime; a leaf mask covers one
    object at one instant. Moving a name from the first class to the second
    reopens the window whatever else the diff says.

    Asserted as EQUALITY rather than containment, so the constant cannot drift in
    either direction unnoticed: a lost hold reddens, and a newly granted one has
    to be recorded in the same diff that grants it.
    """

    @pytest.mark.parametrize("tier", TIERS)
    def test_held_set_matches_the_recorded_set(self, tier: str) -> None:
        held, _ = _split(tier)
        held_leaves = {
            leaf
            for leaf in HELD_BY_ENCLOSING_MASK
            if any(path.endswith("/" + leaf) for path in held)
        }
        lost = sorted(HELD_BY_ENCLOSING_MASK - held_leaves)
        assert not lost, (
            f"on {tier} these names lost their enclosing stand-in mask and are now held "
            f"only by their own leaf name: {lost}. A host-side atomic replace of such a "
            "name puts a writable object at a protected path for the rest of that "
            "namespace's life."
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
    EXPECTED: dict[str, int] = {"standard": 235, "cc": 242, "strict": 243}

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

    @pytest.mark.parametrize("tier", TIERS)
    def test_every_protected_name_is_accounted_for(self, tier: str) -> None:
        held, leaf_only = _split(tier)
        assert len(held) + len(leaf_only) == len(_protected(tier))
