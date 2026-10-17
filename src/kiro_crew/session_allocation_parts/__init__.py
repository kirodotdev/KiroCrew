"""The owner modules of ``kiro_crew.session_allocation``, lowest layer first.

* ``errors`` -- the refusals the allocation boundary raises, and the ending fence's wait
  bound.
* ``companions`` -- companion and run runtimes: the shared processes a parent session's
  subagents and a run's task sessions run on.
* ``get_or_create`` -- ``SessionAllocationService.get_or_create``: claim the live session
  for a key, or cold-start one.

``kiro_crew.session_allocation`` imports every module here, binds their
``SessionAllocationService`` members on the class, and stays the import path for every
project name it bound before they existed. A module here never imports it at run time.
"""
