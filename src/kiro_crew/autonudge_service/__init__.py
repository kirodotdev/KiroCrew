"""The AutoNudge service's owners, composed by :class:`kiro_crew.autonudge.AutoNudgeService`.

``AutoNudgeService`` stays the one object callers hold. It keeps the live loop registry
and the runtime coordination state every owner below shares -- the service lock, the
per-loop timer tasks, the fire window, the charge-on-delivery claims, the structured
correlations -- because a lock order and a delete-wins rule only hold where one object
owns them. It also keeps its lifecycle and singleton publication, and the loader that
vets every stored row at the trust boundary. Each module here owns one responsibility
and, at import time, imports only modules listed above it:

* :mod:`~kiro_crew.autonudge_service.model` -- the loop record, its stop-reason
  vocabulary and the predicates over one record;
* :mod:`~kiro_crew.autonudge_service.subject` -- which pull request a loop is about
  (monitor inference) and the digest of one reading of it;
* :mod:`~kiro_crew.autonudge_service.store` -- ``autonudge.json`` and its quarantine
  sidecar: :class:`~kiro_crew.autonudge_service.store.LoopStore` owns the durable
  store's state and file protocol;
* :mod:`~kiro_crew.autonudge_service.maintenance` -- the per-data-home transaction
  lock, the maintenance quiesce and the maintenance view;
* :mod:`~kiro_crew.autonudge_service.timers` -- the per-loop timer tasks, the
  turn-lifecycle hooks that arm them and the reconciler that rescues a stranded one;
* :mod:`~kiro_crew.autonudge_service.gate` -- the probe gate: whether a tick spends a turn;
* :mod:`~kiro_crew.autonudge_service.judge_tick` -- the wake judge's screening of a
  tick and the labelling of its verdicts;
* :mod:`~kiro_crew.autonudge_service.firing` -- one tick's terminal bounds, the fire
  and its bookkeeping, and the manual trigger;
* :mod:`~kiro_crew.autonudge_service.mutations` -- the legacy loop transactions: arm,
  update, deactivate and remove;
* :mod:`~kiro_crew.autonudge_service.monitor_records` -- the structured-monitor
  transitions the typed controller drives.

No module but ``store`` holds state of its own. Every function that takes the service
as ``self`` is an ``AutoNudgeService`` method, bound on the class by name
(``_timer = firing._timer``) in one grouped table, and those methods reach each other
only through ``self``. The rest are plain helpers the owners import directly: the record
predicates in ``model`` and ``subject``, the lock helpers in ``maintenance`` and two small
ones in ``timers``; a patch on the facade's binding of one of those does not reach an
owner that imported it. ``kiro_crew.autonudge``
stays the import and patch surface: every name these modules hold is re-exported there
as the same object. The names moved code reads through that facade at call time -- a
function-local import, so the graph stays acyclic at import time -- are the ones tests
patch there (``_OVERDUE_REARM_SECS``, ``_RECONCILE_INTERVAL_SECS``,
``replace_with_retry``, ``fsync_dir``) and the ones that must stay defined in it
(``scrubbed_judge_spec``, ``_INSTANCE``, ``_MAINTENANCE_LOCKS``,
``_MUTATION_LOCK_OWNERS``).
"""
