"""The members of :class:`kiro_crew.cron.CronService`, one owner per responsibility.

``CronService`` stays defined in :mod:`kiro_crew.cron` with its state (``__init__``), the
``_claims`` property, the loop-safety guard and the store-lock cores whose log lines carry
code-scanning alerts keyed by that file's path; every other member is a module-level
function here whose first parameter is ``self`` (``cls`` for ``create``; the three static
members ``_handles_of``, ``_apply_held_run_record`` and ``_snapshot`` take neither), and
the class binds it by name (``cancel = _run_teardown.cancel``). So a member is the same function object whether it is read
on the class, an instance or its owner, a patch of it on the class or an instance
reaches every caller, and the service's state stays on the instance.

* :mod:`~kiro_crew.cron_service.service.service_lifecycle` -- starting and stopping the service;
* :mod:`~kiro_crew.cron_service.service.run_teardown` -- the reaper and ``cancel``;
* :mod:`~kiro_crew.cron_service.service.run_kill` -- ending a run's processes and sessions;
* :mod:`~kiro_crew.cron_service.service.job_writes` -- adding and changing jobs;
* :mod:`~kiro_crew.cron_service.service.job_removal` -- removing jobs;
* :mod:`~kiro_crew.cron_service.service.job_ownership` -- who owns a job;
* :mod:`~kiro_crew.cron_service.service.run_registry` -- the runs in flight;
* :mod:`~kiro_crew.cron_service.service.wake_timer` -- when the scheduler wakes;
* :mod:`~kiro_crew.cron_service.service.run_execution` -- running one job;
* :mod:`~kiro_crew.cron_service.service.run_merge` -- landing a run's result;
* :mod:`~kiro_crew.cron_service.service.stall_breaker` -- the loop-stall breaker;
* :mod:`~kiro_crew.cron_service.service.store_sync` -- the service's copy of the cron store.

These modules never import :mod:`kiro_crew.cron` at import time (it imports them). A
name tests patch on ``kiro_crew.cron``, and a name that module defines, is read
through it on each call, ``from kiro_crew import cron as seams`` then
``seams.<name>``, so a patch there reaches the moved code as it did before the
members moved; ``test/test_cron_service_bindings.py`` pins the bindings and that reach. A
blocking call reached through ``seams`` (``seams.time.sleep``) is one the sync-io gate
cannot see, so that test refuses one inside an ``async def`` here.
"""
