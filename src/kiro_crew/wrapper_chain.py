"""The provider/client/handle wrappers nested under a provider, in walk order.

One walk for every reader that needs the state a session keeps at the bottom of
its wrapper stack: the usage rows' resolved model and agent
(``dashboard/handlers/usage.py``) and the engine-fallback tracker
(``acp/engine_fallback.py:tracker_of``). Pure: it imports nothing from the rest
of Kiro Crew, so the ACP layer and the dashboard can both depend on it.
"""

from __future__ import annotations

# Runaway guard for :func:`wrapper_chain`, NOT a depth limit. The walk follows
# the four documented holder attributes and stops at objects it has already
# seen, so a real provider stack — a handful of wrappers around one handle and
# one runtime — is exhausted long before this. What the bound exists for is a
# source that SYNTHESIZES attributes: a ``MagicMock`` answers every ``getattr``
# with a fresh child, so without a bound the walk would never end. It is sized
# so that no plausible wrapper depth can reach it: a wrapper layer carrying all
# four holders costs three nodes on the way down, so this admits twenty-plus
# stacked layers, an order of magnitude past anything a session accumulates.
WRAPPER_CHAIN_MAX_NODES = 64

# Holder attributes, in the order they are followed. ``_runtime`` is LAST on
# purpose — see :func:`wrapper_chain`.
WRAPPER_HOLDERS: tuple[str, ...] = ("client", "_client", "_handle", "_runtime")


def wrapper_chain(source: object) -> list[object]:
    """Collect *source* and the provider/client/handle wrappers nested under it.

    ``providers/acp.py`` keeps its inner object on ``_client`` (also exposed as
    ``client``), ``acp/session_provider.py`` keeps the session handle on
    ``_handle``, and both the handle and the provider keep the spawned CLI
    runtime on ``_runtime`` — and these nest, because ``providers/acp.py``
    assigns an ``AcpSessionProvider`` to ``_client``. A default Kiro turn
    therefore hides its resolved state two levels down, at
    ``provider.client._handle``, so probing a fixed depth misses it.

    Depth-first along the holder attributes, with an identity-based visited set
    so a wrapper that points back at itself (or two wrappers that share a
    runtime) terminates. Depth-first, not breadth-first, because the session's
    state sits at the BOTTOM of the holder chain while every wrapper layer above
    it — a fallback wrapper, session sharing, a subagent companion, a
    channel-linked session — carries siblings that hold nothing. A breadth-first
    walk with a node budget spends that budget on the siblings and, a few layers
    down, stops short of the handle. Following the chain downward first reaches
    the handle regardless of how many layers are stacked on top;
    :data:`WRAPPER_CHAIN_MAX_NODES` is a runaway guard for attribute-synthesizing
    sources, not a depth limit.

    ``_runtime`` is traversed **last** deliberately. It is the only holder of
    ``_agent`` for the session-provider shape, but it also carries the
    process-level ``--model`` argument; visiting it after the whole ``_handle``
    subtree keeps session-level model state ahead of process-level state for a
    reader that falls through to ``_model``.
    """
    chain: list[object] = []
    # A stack: the LAST entry is visited next, so children are pushed in
    # reverse holder order to come off in holder order.
    pending: list[object] = [source]
    while pending and len(chain) < WRAPPER_CHAIN_MAX_NODES:
        node = pending.pop()
        if node is None or any(seen is node for seen in chain):
            continue
        chain.append(node)
        for holder in reversed(WRAPPER_HOLDERS):
            inner = getattr(node, holder, None)
            if inner is not None and not isinstance(inner, (str, bytes, int)):
                pending.append(inner)
    return chain
