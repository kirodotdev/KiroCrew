"""Personal Remote Control: the one setting, and turning it off.

* :mod:`~kiro_crew.remote_control.defaults` -- the one default value, a leaf module
  the config schema imports.
* :mod:`~kiro_crew.remote_control.setting` -- reads and writes
  ``tunnel.relay_enabled``, the one on/off control.
* :mod:`~kiro_crew.remote_control.guard` -- in the gateway, stops the running
  relay the moment the setting is off.
* :mod:`~kiro_crew.remote_control.cli` -- ``kirocrew remote-control``.

Ending the lease with the lease service is not here; the relay client owns it.

This ``__init__`` imports nothing, so the config schema's import of ``defaults``
stays a leaf.
"""
