"""The owner modules :mod:`kiro_crew.agent_discovery` is composed from.

``kiro_crew.agent_discovery`` is the import path and the patch surface of agent discovery:
callers import ``list_agents`` and the spec readers from it, and the tests patch them
there. Each module here owns one responsibility; the facade imports them lowest layer
first, and a module imports only the ones before it, never the facade:

* ``spec_reads`` -- the user-level agents directory and the fenced read of one spec in
  it: the sensitive-path and UNC gates, the pinned read, the denial audit, the head
  probes, the lookups by declared name and stem, and the scan-failure warning;
* ``spec_fields`` -- the discovery record (``AgentInfo``, the two scopes) and what one
  spec's fields read as: its model, its skill resources and URIs, its welcome message;
* ``spec_snapshot`` -- the stat-only directory signatures, the caches they validate (the
  ``list_agents`` result, the project-name index, the parsed-spec snapshot) and
  ``clear_list_agents_cache``, which drops the result cache and the snapshot;
* ``dir_revision`` -- the content revision of an agents directory and the memo that keys
  derived answers on it;
* ``project_agents`` -- agents a checkout declares: the scope fence, the scan, the
  declared names and their cached, warmed index;
* ``agent_roster`` -- ``list_agents`` and the rows it builds, the edition seam, the
  name-to-model map (its own scan of the agents directory) and the welcome message of the
  spec the roster resolves;
* ``skill_scope`` -- which skill trees an agent and a session may load: why a spec
  withholds ``skill_search``, inheritance, the resolved globs, and the served-harness label
  that gates inheritance.
"""
