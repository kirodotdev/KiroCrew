"""Owner modules of ``kiro_crew.session_lifecycle``.

Each module holds one responsibility of the session lifecycle service: the module-level
stop-decline and parked-queue records, and the bodies of ``SessionLifecycleService``'s
members as module-level functions whose first parameter is ``self``, which
``session_lifecycle.SessionLifecycleService`` binds by name. Import from
``kiro_crew.session_lifecycle``, never from here
(``docs/system-specs/modules/session.md`` § Implementation Boundaries).
"""
