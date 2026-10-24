"""Owners composed by :mod:`kiro_crew.dashboard.state`.

``kiro_crew.dashboard.state`` stays the import path and the patch surface of the
dashboard's shared state: callers and tests import and patch names there, never
here. The facade imports every owner below, eagerly, lowest layer first, and installs
:mod:`kiro_crew._facade_mirror`, so a ``monkeypatch.setattr`` or ``mock.patch`` of
``kiro_crew.dashboard.state.<name>`` reaches every owner that reads that name, exactly
as it reached the one module before the split. Two rules follow:

* An owner never imports the facade (``kiro_crew.dashboard.state``), not even under
  ``TYPE_CHECKING``; at run time it imports only owners listed before it. An
  annotation that names a class another owner defines (``DashboardState``,
  ``_ChatSlot``) imports it from that owner under ``TYPE_CHECKING`` only.
* A module global that a function rebinds with ``global`` lives in exactly one owner
  and no other module imports it; the facade serves it live through the mirror.

A class split across files is BOUND, not inherited (``dashboard_state.py`` holds
``DashboardState`` the same way): ``chat_slot.py`` holds the ``_ChatSlot`` class
(its ``__slots__``, field declarations, constructor and
properties) and binds each method as a one-line class attribute
(``set_todo = _slot_todo.set_todo``). The method's body lives, unchanged, in the
collaborator that owns that responsibility, as a function whose first parameter is
``self``. The class attribute IS that function, so its signature, source, coroutine
kind and identity are what callers, ``MagicMock(spec=...)`` and ``patch.object``
see, and the slot keeps every field on itself: collaborators own behaviour and the
initial values of their fields (``init_<x>_fields``), never the storage. Each
collaborator's docstring lists the slot fields it reads and writes.
``DashboardState`` binds the slot-list hold rules the same way: their bodies live
in ``slot_list_hold.py`` and ``dashboard_state.py`` carries one binding line per method.

Where new code goes: a helper of one responsibility goes to the owner named for it
(the first line of each module's docstring); a new ``_ChatSlot`` or
``DashboardState`` method goes to its collaborator and gets one binding line in its
shell (``chat_slot.py`` or ``dashboard_state.py``); a new slot field gets a
``__slots__`` entry and a declaration in ``chat_slot.py`` and its default in the
collaborator's ``init_<x>_fields``.

A bound function annotates ``self`` so mypy checks its body as a method body; once
the class is built, :func:`kiro_crew._facade_bindings.own_methods` drops that
annotation from the runtime object and names the function after the class attribute
(``_ChatSlot.append``), so its ``inspect.signature``, its ``__qualname__`` (which
stall reports and live-config callback names read) and its coroutine kind are what the
one-module class had. Only
``__module__`` moves: it names the collaborator that holds the body. A type hint
resolves in that collaborator's globals, so ``typing.get_type_hints`` cannot resolve
a hint naming a class the module imports only under ``TYPE_CHECKING``;
``test/test_state_parts_facade.py`` (``_ChatSlot``) and
``test/test_state_parts_dashboard_state.py`` (``DashboardState``) name the members
that differ.
"""

from __future__ import annotations
