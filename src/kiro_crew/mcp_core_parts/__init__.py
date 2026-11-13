"""The owner modules :mod:`kiro_crew.mcp_core` is composed from.

``kiro_crew.mcp_core`` is the kirocrew-core MCP server's import path and its patch
surface: the tool handlers in :mod:`kiro_crew.mcp_tools` read its plumbing as
attributes of that module, and the tests patch it there. Each module here owns one
responsibility; the facade imports them lowest layer first, and a module imports only
the ones before it, never the facade:

* ``identity`` -- which session is calling: the lenient and the strict resolvers, the
  fail-closed gate and its diagnosis, the reflexive-module registry;
* ``gateway`` -- where a call goes and the call itself: target resolution, its caches,
  the refused-target recovery, the internal secret, the identity headers, ``_send`` with
  its retry schedule, failure text and error body, the five verbs built on it, and the
  inbound redaction of a forwarded JSON body. The other own servers import the verbs
  from here, so they never load the core server;
* ``governance`` -- channel-agent containment and the governance vets;
* ``crew`` -- Crew Mode's ``route_crew`` and ``select_crew``;
* ``history`` -- the chat-history search helpers.

The facade serves every name these modules define, and its mirror
(:mod:`kiro_crew._facade_mirror`) carries a patch of ``kiro_crew.mcp_core.<name>`` to
every module that binds it, so the moved code reads its own globals unchanged.
"""
