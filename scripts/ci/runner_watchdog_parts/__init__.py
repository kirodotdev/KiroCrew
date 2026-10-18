"""The owner modules of ``scripts/ci/runner_watchdog.py``, one per stage a tick runs through.

``runner_watchdog`` is the script the workflow runs and the only module outside this
package that imports these. It keeps the knobs, the verdict vocabulary, the tick driver
(``run_watchdog``), the kicked-tick debounce (``kick_is_redundant``, ``stand_down``), the
step summary and the entry point, and re-exports every name the owners define.
``client`` is the GitHub REST client; ``classify`` judges one run from its own payload
and jobs; ``heal_safety`` decides whether a workflow's runs may be cancelled and re-run;
``listing`` reads the runs and jobs a tick judges within its read budget;
``supersession`` asks whether a newer run carries the run's branch or pull request;
``heal`` cancels and re-runs orphans; and ``recovery`` re-runs the cancelled orphans
nobody re-ran. An owner imports only the owners before it in that list.

A name the tests patch on ``runner_watchdog`` is read by every owner, the one that
defines it included, as ``runner_watchdog.<name>`` at call time. The only binding of it
an owner may hold is the ``def`` or ``class`` that defines it, so a patch reaches every
reader.
"""
