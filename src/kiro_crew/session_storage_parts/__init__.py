"""The owner modules of :mod:`kiro_crew.session_storage`.

``kiro_crew.session_storage`` is the only import path: it imports these owners, lowest
layer first, binds every name they define but the two caches ``store_inventory`` rebinds,
and serves those and the names the owners import. Each owner
imports only the owners before it, and none imports ``kiro_crew.session_storage``. The
owner of each responsibility is listed in the facade's foot and in
``docs/system-specs/modules/session-storage.md`` (Module layout).
"""
