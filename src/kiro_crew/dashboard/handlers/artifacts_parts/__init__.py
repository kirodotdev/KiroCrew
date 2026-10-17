"""Owner modules of :mod:`kiro_crew.dashboard.handlers.artifacts`.

``dashboard/handlers/artifacts.py`` is the import path and the patch seam of the
artifact HTTP routes; the code lives here, one responsibility per module, and new code
for a responsibility goes to its module. The facade's mirror
(:mod:`kiro_crew._facade_mirror`) sends a write through ``handlers.artifacts`` to every
module below that binds the name, so a patch of ``handlers.artifacts.<name>`` reaches
every reader. This package re-exports nothing.
"""
