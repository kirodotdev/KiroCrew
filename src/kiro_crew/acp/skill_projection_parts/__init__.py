"""The owner modules :mod:`kiro_crew.acp.skill_projection` is composed from.

``kiro_crew.acp.skill_projection`` is the import path and the patch surface of Crew's
native skill startup views: the ACP transports and the doctor import them from it, and
the tests patch them there. Each module here owns one responsibility; the facade imports
them lowest layer first, and a module imports only the ones before it, never the facade:

* ``side_files`` -- the projection's side files under this data home and the protocol
  over them: their layout and legacy locations, the alias-name shape, the bounded
  publication lock, the per-process leases with their liveness scan, and the
  identity-checked unlinks;
* ``inheritance_overlay`` -- Crew's overlay on Kiro's default-resource inheritance in a
  workspace ``cli.json``: its keys, the settings read, the recorded preference and the
  rollback;
* ``view_publication`` -- what a view hands kiro-cli beyond its authored spec (bounded
  display text, the search tool) and the in-place rewrite that makes kiro-cli reload it;
* ``view_ownership`` -- which views are this data home's: ownership records, the home
  tag, view identity and digest, sidecar reads, and whether a published copy is current;
* ``view_sources`` -- which agent a skill-view name projects: the in-process map, the
  view ledger, the sidecar fallback and the resolvers;
* ``native_projection`` -- the prepared ``NativeSkillProjection`` and the registry of
  live projections a prune must keep;
* ``alias_reclaim`` -- reclaiming unused views and their residue (the per-spawn,
  end-of-projection and boot prunes, the sweeps and the legacy migration) and the
  read-only censuses the doctor reports;
* ``preparation`` -- the spawn-time entry point, ``prepare_native_skill_projection``,
  with its view-count ceiling fallback.

The facade serves every name these modules define, and its mirror
(:mod:`kiro_crew._facade_mirror`) carries a patch of
``kiro_crew.acp.skill_projection.<name>`` to every module that binds it, so the moved
code reads its own globals unchanged.
"""
