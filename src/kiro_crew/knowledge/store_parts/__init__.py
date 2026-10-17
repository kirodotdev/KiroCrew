"""The owners ``kiro_crew.knowledge.store`` composes ``KnowledgeStore`` from.

``kiro_crew.knowledge.store`` stays the import path of the knowledge store: callers and
tests import ``KnowledgeStore`` and the store's module-level names from there, never from
a module here. ``KnowledgeStore`` is split across these owners by binding, not
inheritance: each method's body is a module-level function in the owner of its
responsibility, whose first parameter is ``self`` (annotated ``KnowledgeStore`` for type
checkers only), and the class body binds it under the same name
(``ensure_fts_index_current = _fulltext_index.ensure_fts_index_current``;
``staticmethod(...)`` for a static method). The class attribute IS that function, so a
patch of a method on the class or on an instance reaches every caller, and every field
stays on the instance.

Rules that keep the split sound:

* An owner never imports ``kiro_crew.knowledge.store`` at run time (only under
  ``TYPE_CHECKING``, for the ``self`` annotation). It imports only owners listed before
  it in ``kiro_crew.knowledge.store._STORE_OWNERS``, lowest layer first.
* An owner reads its globals in its own namespace, so the facade re-exports every name
  callers import from it (its own standard-library and typing imports aside) rather than
  mirroring writes to them. A name a test patches through the facade is read only by code
  the facade keeps (``test/test_store_parts_bindings.py`` holds that against the test
  trees).
* An owner that logs does so under the ``kiro_crew.knowledge.store`` logger.
* A function that names ``KnowledgeStore`` itself, the connection, the constructor and
  the class's properties stay in ``knowledge/store.py``.

Where new code goes: a new store method goes to the owner whose first docstring line
names its responsibility, with one binding line under that owner's header in the class, a
row in ``_MEMBERS`` in ``test/test_store_parts_bindings.py``, and the owner's
``Writes:``/``Reads:`` line updated when it touches a new field; a helper or constant
goes beside the methods that read it. A method none of them owns is a new owner, listed
in ``_STORE_OWNERS``.
"""
