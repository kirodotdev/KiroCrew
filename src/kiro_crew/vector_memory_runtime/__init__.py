"""Owners composed by :class:`kiro_crew.vector_memory.VectorMemoryStore`.

The store keeps the single SQLite connection, its ``_db_lock`` and the write
paths the store's own guards pin to ``vector_memory.py``; the modules here hold
the retrieval, ranking, repair and parsing rules it delegates to. Each function
that operates on a store takes it as its first argument, and every statement it
runs goes through ``store.db`` under ``store._db_lock``, so no second connection,
lock or repository exists.

This package imports nothing on its own: ``kiro_crew.vector_memory`` imports
every submodule when it loads, and submodules read the facade's patch seams
(``np``, ``faiss``, ``_now_iso``, ...) through that module at call time.
"""
