"""Private owners composed by :mod:`kiro_crew.telegram.transport_dispatch`.

``TelegramDispatcher`` keeps its state, its inbound front door, ``/stop``, ``/title``,
the restriction predicates, the user-facing failure reason and the
conversation-identity helpers (route, session key, rotation, agent): repository guards
read those in ``transport_dispatch.py`` by path. The modules here hold its other
responsibilities, the turn engine (``turn_engine``) and the end-of-turn queue drain
with its receipt wrappers (``turn_queue``) among them.

Every owner follows the same placement rules:

* A function whose first parameter is ``self`` is bound as the ``TelegramDispatcher``
  attribute of the same name, so patches, ``inspect.getsource`` and ``self.<name>``
  reach it like a method defined in the class body.
* A name tests patch on the facade (``sel``, ``TelegramApprovalDecider``,
  ``list_agents``, ...) is read through ``kiro_crew.telegram.transport_dispatch`` at
  call time, with a function-local import, and never bound in an owner;
  ``test/test_telegram_transport_dispatch_composition_contract.py`` derives that set
  from the tests and fails on an owner binding one.
* An owner imports the facade at module level only under ``TYPE_CHECKING``, reads a
  sibling's helpers as module attributes, and logs under the facade's logger name.

This package imports nothing on its own: the facade imports every owner when it loads.
"""
