"""Owner modules of :mod:`kiro_crew.dashboard.handlers.sessions`.

``handlers/sessions.py`` is the import path and the patch seam of the session routes; the
code it does not keep lives here, one responsibility per module, and new code for a
responsibility goes to its module. The facade's mirror (:mod:`kiro_crew._facade_mirror`)
sends a write through ``handlers.sessions`` to every module below that binds the name, so a
patch of ``kiro_crew.dashboard.handlers.sessions.<name>`` reaches every reader. No module
here imports the facade at run time, and this package re-exports nothing.
"""
