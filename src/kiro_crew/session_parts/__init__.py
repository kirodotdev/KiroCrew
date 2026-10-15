"""The owner modules ``kiro_crew.session`` is composed from.

``kiro_crew.session`` stays the import path, the patch target and the home of
``SessionManager``; each module here owns one responsibility of the manager and is
imported by the facade alone. New work goes to the module that owns its
responsibility (see ``docs/system-specs/modules/session.md``, "Implementation
Boundaries").
"""
