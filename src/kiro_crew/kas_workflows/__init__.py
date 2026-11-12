"""KAS-native workflows: Crew drives and mirrors the harness's own workflow engine.

When the session backend carries ``SessionCapabilities.native_workflows``, KAS
owns workflow definitions ("recipes"), orchestration and run history. Crew talks
to it over the ``_kiro/workflow/*`` extension methods and keeps no second durable
store. What Crew does keep is an in-memory mirror of run rows, rebuilt from KAS
on boot, which every dashboard surface reads.

- :mod:`.wire` -- method and notification names, tolerant parsers, and the
  run-row control rules every surface renders.
- :mod:`.mirror` -- the reducer that folds lifecycle notifications and list or
  load answers into run rows.
"""
