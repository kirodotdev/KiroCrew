"""Owners composed by :class:`kiro_crew.vector_memory.VectorMemoryStore`.

The store keeps the shared SQLite connection and its ``_db_lock``; the modules here
hold the retrieval, ranking, repair and parsing rules it delegates to, and the write
paths, bookkeeping and derived-state rules it binds as members. Each function that
operates on a store takes it as its first argument (``store`` for a delegated rule,
``self`` for a bound member), and every statement it runs goes through the store's
``db`` under its ``_db_lock`` or through its fetch helpers (which may read on a
pooled read-only connection), so no module opens a connection, lock or repository.

This package imports nothing on its own: ``kiro_crew.vector_memory`` imports
every submodule when it loads, and submodules read the facade's patch seams
(``np``, ``faiss``, ``_now_iso``, ...) through that module at call time.
"""
