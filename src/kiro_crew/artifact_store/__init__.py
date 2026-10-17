"""Owners behind the :mod:`kiro_crew.artifacts` facade.

``model`` holds the record dataclasses and errors, ``rules`` the field grammar and
kind policy, ``images`` the raster allowlist and header sniffing, ``records`` the
persisted file formats, ``comments`` the comment-thread rules and ``folders`` the
folder tree. Import from :mod:`kiro_crew.artifacts`: it keeps its whole import
surface by re-exporting each moved name with one identity, and owns the store,
its lock and the default singletons. The ``records`` and ``comments`` helpers
are the store's internals.
"""
