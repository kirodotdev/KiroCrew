"""Which crews get a turn URL and no dashboard token, keyed on the CREW.

``registry.is_headless_provisioner`` is the one judge of that, and the connect
path and the self-heal prime ask it directly because they hold the instance row.
These cover the sites that do not: ``_TransportParams.turn_url`` has ``self`` and
a port and nothing else, and the two mint refusals and the restart refusal are
reached with only the resolved params in hand. So the answer is carried on the
params as ``headless``, set once where they are built.

Every one of those sites read ``method == "fargate"``, and every one was wrong
the same way: the Fargate lane happens to be headless, so ``"fargate"`` stood in
for ``headless`` wherever the latter was meant. A MicroVM crew is headless and
reaches its guest over ``ssm``, so it failed all of them -- most visibly in
self-heal tier 2, which took the re-MINT arm, raised, and stood down, leaving a
dropped forward unrebuilt.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# What decides that a crew is chatted with rather than embedded
# ---------------------------------------------------------------------------
#
# ``registry.is_headless_provisioner`` is the one judge, and the connect path and
# the self-heal prime already ask it. These cover the sites it could not reach,
# because they have no instance row in scope -- ``_TransportParams.turn_url`` has
# ``self`` and a port and nothing else -- so the answer is carried on the params
# as ``headless``.
#
# All of them were ``method == "fargate"``, and all were wrong the same way: the
# Fargate lane happens to be headless, so "fargate" read as a proxy for
# "headless". A MicroVM crew is headless too and reaches its guest over ``ssm``,
# so it failed every one of them.


def _params(method: str, headless: bool):
    from kiro_crew.instances.ssh_tunnel_manager import _TransportParams

    return _TransportParams(method=method, ssh_host="127.0.0.1", headless=headless)


@pytest.mark.parametrize(
    ("method", "headless"),
    [("fargate", False), ("fargate", True), ("ssh", True), ("ssm", True)],
)
def test_a_headless_crew_gets_a_turn_url_whatever_its_transport(method, headless):
    assert _params(method, headless).turn_url(18200) == (
        "http://127.0.0.1:18200/v1/chat/completions"
    )


@pytest.mark.parametrize("method", ["ssh", "ssm"])
def test_a_gateway_crew_still_gets_no_turn_url(method):
    """The other direction, so the flag is not vacuously true.

    A crew with a dashboard is reached by embedding it. Giving that forward a turn
    URL would tell the pane to post a crew turn at a gateway's own port.
    """
    assert _params(method, headless=False).turn_url(18200) == ""


def test_the_params_flag_is_the_registry_s_own_answer():
    """``headless`` carries ``is_headless_provisioner``, it does not re-decide it.

    Two spellings of one question is how the halves came to disagree in the first
    place, so the flag must be derivable from the function rather than from a
    second list of lanes.
    """
    from kiro_crew.instances.registry import (
        HEADLESS_CREW_PROVISIONERS,
        is_headless_provisioner,
    )

    assert all(is_headless_provisioner(p) for p in HEADLESS_CREW_PROVISIONERS)
    assert not is_headless_provisioner("builtin")
    assert not is_headless_provisioner("")


def test_every_resolved_transport_carries_the_flag():
    """The flag is set where the params are built, for all four shapes.

    A construction site that forgets it defaults to ``False``, which is the
    silent version of the bug: a headless crew that looks like a gateway crew.
    """
    import ast
    import inspect

    from kiro_crew.instances import ssh_tunnel_manager as mod

    tree = ast.parse(inspect.getsource(mod))
    built = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_TransportParams"
    ]
    assert len(built) >= 4, f"expected the four resolver sites, found {len(built)}"
    for node in built:
        kwargs = {kw.arg for kw in node.keywords}
        assert "headless" in kwargs, f"_TransportParams at line {node.lineno} omits headless"
