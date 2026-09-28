"""The cron service's owners, composed by :class:`kiro_crew.cron.CronService`.

Each module owns one responsibility and, at import time, imports only modules
listed above it:

* :mod:`~kiro_crew.cron_service.schedule` -- when a job fires (next run, due, jitter,
  rendering, time parsing);
* :mod:`~kiro_crew.cron_service.model` -- the job record itself;
* :mod:`~kiro_crew.cron_service.identity` -- the session key a run presents, the
  principal it names, the memory it runs with;
* :mod:`~kiro_crew.cron_service.claims` -- a run's occupancy of its job;
* :mod:`~kiro_crew.cron_service.execution` -- a run's deadlines and terminal record;
* :mod:`~kiro_crew.cron_service.store` -- ``crons.json``'s format, lock and failure types;
* :mod:`~kiro_crew.cron_service.fields` -- what a valid job and a valid update are;
* :mod:`~kiro_crew.cron_service.readers` -- read-only views of the store with no
  scheduler running;
* :mod:`~kiro_crew.cron_service.folders` -- cron folder definitions.

:mod:`kiro_crew.cron` stays the import and patch surface: every name these modules
hold is re-exported there as the same object, and a name tests patch there is read
by these modules through it on each call (a function-local import, so the module
graph stays acyclic at import time).
"""
