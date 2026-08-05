# `transfer/` — data migration tools

This folder holds standalone scripts and tools for **moving user data between
installs or formats** — e.g. importing a legacy install's sessions, memory, and
settings into the current KiroCrew home.

These are intentionally kept *out* of the product UI: they target a small subset
of users (e.g. those upgrading from a previous app), so shipping them as one-off
scripts keeps the main Settings surface uncluttered for the typical public user.

## Layout

Each tool lives in its own subfolder so they stay self-contained and new ones
drop in without collisions:

```
transfer/
  README.md                     # this file
  meshclaw_to_kirocrew/         # import data from a legacy MeshClaw install
    engine.py                   # pure migration logic (unit-tested)
    migrate.py                  # runnable CLI wrapper
    test_engine.py              # unit tests
    README.md                   # how to run this specific tool
```

## Conventions for a new transfer tool

1. Create a subfolder named `<source>_to_<target>/`.
2. Put the testable logic in `engine.py` (no UI/web deps; safe to import).
3. Provide a thin `migrate.py` CLI with `--dry-run` and a confirmation prompt.
4. Add `test_engine.py` covering detect / dry-run / copy / skip / idempotency.
5. Add a `README.md` with run instructions and what is copied vs. skipped.

Tests under `transfer/` are picked up by the repo's pytest run (see `setup.cfg`
`testpaths`).
