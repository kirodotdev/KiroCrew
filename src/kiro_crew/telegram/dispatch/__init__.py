"""Private owners composed by :mod:`kiro_crew.telegram.transport_dispatch`.

``TelegramDispatcher`` keeps its state, its inbound front door and turn engine, the
end-of-turn queue drain and the receipt wrappers it shares with the steer path,
``/stop``, ``/title``, the durable transcript write and the conversation-identity
helpers (route, session key, rotation, agent): repository guards read those in
``transport_dispatch.py`` by path. The modules here hold its other responsibilities.

Every owner follows the same placement rules:

* A function whose first parameter is ``self`` is a dispatcher method. The facade
  binds it as the ``TelegramDispatcher`` attribute of the same name, so instance and
  class patches, ``inspect.getsource`` and every ``self.<name>(...)`` call site reach
  it exactly as they reach a method defined in the class body. It works on the
  dispatcher's own state and reaches other dispatcher behaviour through ``self``.
* A name tests patch on the facade (``sel``, ``TelegramApprovalDecider``,
  ``list_agents``, ...) is read through ``kiro_crew.telegram.transport_dispatch`` at
  call time, with a function-local import, and never bound in an owner;
  ``test/test_telegram_transport_dispatch_composition_contract.py`` derives that set
  from the tests and fails on an owner binding one.
* An owner imports the facade at module level only under ``TYPE_CHECKING``, reads a
  sibling's helpers as module attributes, and logs under the facade's logger name.

This package imports nothing on its own: the facade imports every owner when it loads.
"""
