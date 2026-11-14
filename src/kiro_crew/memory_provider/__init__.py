"""Pluggable memory providers: one seam, Crew's own memory and app-served providers.

- :mod:`.contract` -- the data a provider exchanges with Crew, its errors and caps;
- :mod:`.resolver` -- ``route_for``, the only selection point, and the per-session
  frozen choice;
- :mod:`.builtin` -- Crew's own memory spoken through the contract;
- :mod:`.app_client` -- the wire client for a provider an App Kit app serves;
- :mod:`.render` / :mod:`.chokepoints` / :mod:`.recall` / :mod:`.lesson_routes` --
  what each memory reader and writer does when a provider answers;
- :mod:`.consent` -- the owner's egress consent (keystone leaf);
- :mod:`.migration` -- persisted move jobs (plan, run, verify, resume, rollback),
  export and import;
- :mod:`.conformance` -- the provider author's conformance kit.

The spec is the "Memory providers" section of
``docs/system-specs/modules/memory-skills-hooks.md``.
"""
