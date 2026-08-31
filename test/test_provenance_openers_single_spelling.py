"""The fence and ``dashboard.state`` must agree on every provenance opener BY CONSTRUCTION.

Before this, ``state`` declared each opener and ``constants`` re-spelled the same bytes into the
fence tuple. Editing one spelling left the other matching the old text, and the fence would keep
promoting a label the recovery path had renamed. There is now one definition per opener.
"""

import re

from kiro_crew import constants
from kiro_crew.dashboard import state


def test_state_does_not_respell_a_fenced_opener():
    """Every opener state exposes and the fence reserves resolves to ONE object."""
    fenced = set(constants._RESERVED_PROVENANCE_PREFIXES)
    shared = {
        name: getattr(state, name)
        for name in dir(state)
        if name.endswith("_PREFIX")
        and isinstance(getattr(state, name), str)
        and getattr(state, name) in fenced
    }
    assert len(shared) >= 15, shared
    for name, value in shared.items():
        assert getattr(constants, name) is value, name


def test_the_fence_tuple_names_its_openers_rather_than_respelling_them():
    """A raw string in the tuple is a second spelling nothing holds equal."""
    source = re.search(
        r"^_RESERVED_PROVENANCE_PREFIXES = \((.*?)^\)",
        open(constants.__file__, encoding="utf-8").read(),
        re.S | re.M,
    )
    assert source, "tuple not found"
    raw = [ln.strip() for ln in source.group(1).splitlines() if ln.strip().startswith('"')]
    # Two openers have no named constant anywhere; every other entry must be a name.
    assert sorted(raw) == ['"[End of cron notification]",', '"[SYSTEM]",'], raw


def test_renaming_an_opener_moves_the_fence_with_it():
    """Behavioural: the fence reads the definition, so it cannot lag a rename."""
    assert state.CONN_RECOVERY_PREFIX in constants._RESERVED_PROVENANCE_PREFIXES
    label = "(recommended) " + state.CONN_RECOVERY_PREFIX + " resume"
    assert constants.strip_recommended_marker(label) == label
