"""Private owners composed by :mod:`kiro_crew.connections.warm`.

``warm.py`` stays the one import surface and the one patch surface: every public function,
every seam a test or caller substitutes, and every site a repository gate pins to that file.
It re-exports each rule defined here, so ``warm.<name>`` is the object below.

- :mod:`.spec_plan` -- the immutable desired plan: what a provider's authorization asks for,
  whether the warm process may activate it, and whether a resident plan still serves.
- :mod:`.start_identity` -- whether a process recorded in a generation marker is still that
  process: PID plus start identity, judged tri-state.
- :mod:`.shared_rows` -- the two-axis ownership of shared mint-table rows and the atomic claim
  that installs them, with the displaced-row disposal and rollback that settle it.
- :mod:`.spec_body` -- the text and fixed names of a warm spec, and the marks of one of ours.
- :mod:`.generation_tree` -- the private generation tree: its root, the owner marker's writer,
  and one directory's creation, binding and proven release.
- :mod:`.mint_process` -- the shared process's primitives: its runtime class, liveness, session
  records, challenge collection and reporting teardown.
- :mod:`.rearms` -- automatic replacement after a process death or a grant invalidation.
- :mod:`.table_flow` -- the request path: one activation becomes table rows, and a Connect
  adopts one.

No module here imports the facade at import time, so each one imports cold. A name the facade
defines, or one a test substitutes on it, is read there at call time.
"""
