"""Security Scanner app — core library.

Module map (filled in across build stages):

- ``knowledge``  Tagged, versioned knowledge store: learned patterns,
                 suppressions, external-report ingestion. (Stage 2)
- ``findings``   Finding model, dedup, scan-history persistence. (Stage 2)
- ``topics``     The 3 v1 security topics and their prompt templates. (Stage 2/3)
- ``scan``       Parallel topic-scan orchestration via spawn_run. (Stage 3)
- ``exploit``    Finding-bound PoC generation + evidence model. (Stage 4)
- ``targets``    Target-adapter interface + KiroCrew pod adapter. (Stage 4)
- ``executor``   Sandboxed PoC execution with time/output/path limits. (Stage 4)
- ``reporter``   Dedup-aware notification of new actionable findings. (Stage 5)

All persistent state lives under the app data dir (``data/``), which is
gitignored and per-install. See ``SECURITY_NOTES.md`` for the safety
constraints every module must uphold.
"""
