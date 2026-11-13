"""The settled-coordinator-target store is bounded like the decisions it partners with."""

from types import SimpleNamespace

from kiro_crew.dashboard import interaction_coordinator as ic
from kiro_crew.shared_crew.decisions import MAX_RETAINED_DECISIONS


def test_store_keeps_as_many_targets_as_the_registry_keeps_decisions():
    state = SimpleNamespace()
    for i in range(MAX_RETAINED_DECISIONS + 5):
        ic._remember_settled_target(state, f"id-{i}", {"slot": "s", "instance": f"in-{i}"})
    # The oldest entries past the registry's cap are evicted, and a request the
    # registry still keeps a decision for still has its target.
    assert ic.settled_coordinator_target(state, "id-4") is None
    assert ic.settled_coordinator_target(state, "id-5") == ("s", "in-5")
    assert ic.settled_coordinator_target(state, f"id-{MAX_RETAINED_DECISIONS + 4}") is not None
    assert len(state._settled_coordinator_targets) == MAX_RETAINED_DECISIONS


def test_an_overlong_id_is_not_retained():
    state = SimpleNamespace()
    long_id = "x" * (ic._MAX_SETTLED_ID_CHARS + 1)
    ic._remember_settled_target(state, long_id, {"slot": "s", "instance": "i"})
    assert ic.settled_coordinator_target(state, long_id) is None
    edge = "y" * ic._MAX_SETTLED_ID_CHARS
    ic._remember_settled_target(state, edge, {"slot": "s", "instance": "i"})
    assert ic.settled_coordinator_target(state, edge) == ("s", "i")
