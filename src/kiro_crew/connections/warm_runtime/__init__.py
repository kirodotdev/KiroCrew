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

No module here imports the facade at import time, so each one imports cold.
"""
