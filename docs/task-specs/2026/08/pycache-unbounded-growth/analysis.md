# Root-cause analysis: `~/.kiro/crew/cache/pycache/` unbounded growth (#3176)

> Archive document — written at fix time. Current behavior lives in
> [security](../../../../system-specs/modules/security.md) (env scrub) and
> [session](../../../../system-specs/modules/session.md) (periodic GC).

## Symptom

`~/.kiro/crew/cache/pycache/` mirrors absolute filesystem paths as `.pyc`
caches and grows without bound: ~80 GB on the reporter's machine, and after a
manual wipe it regrew to **8.1 GB / 435,324 files within half a day** under
heavy use with frequent subagent spawns. The bulk was a path-for-path mirror
of the reporter's uv-managed CPython 3.11 (stdlib + site-packages), e.g.
`pycache/Users/<me>/.local/share/uv/python/cpython-3.11.15-.../lib/python3.11/...`.
Deleting the directory caused no breakage.

## Mechanism

1. **Where the directory comes from.** The desktop app
   (`website/electron/main.js`) spawns the gateway with
   `PYTHONPYCACHEPREFIX=<data home>/cache/pycache`. That is deliberate and
   must stay: without it the embedded interpreter writes `__pycache__/*.pyc`
   next to the bundled sources on first import, breaking the codesign seal
   ("a sealed resource is missing or invalid"), which fails Gatekeeper and
   can corrupt Squirrel updates.

2. **Why it applied to far more than the bundle.** Environment variables are
   inherited. The gateway passes its environment to every child it spawns —
   including the kiro-cli agent process, whose subtree runs the agent's bash
   commands, foreign MCP servers, and subagents. Any CPython in that subtree
   (the user's uv-managed interpreters, project venvs, `uv run` ephemeral
   environments) honors the prefix and, per PEP 3147 / `sys.pycache_prefix`,
   mirrors the **absolute path of every module it imports** under the crew
   home instead of writing `__pycache__` beside its own sources.

3. **Why it was unbounded rather than merely large.** A single interpreter's
   mirror is bounded (same paths overwrite in place). But ephemeral
   environments — `uv run`/`uvx` caches, per-task venvs spawned by subagents —
   live at **unique paths**, so each one mints a fresh, never-reused mirror of
   its whole stdlib + site-packages. CPython only ever *adds* to a pycache
   prefix; nothing in CPython, and (before this fix) nothing in Kiro Crew,
   ever deleted from it. Heavy subagent use therefore produced multiple GB of
   distinct entries per day.

4. **Why deletion is harmless.** A `.pyc` is a derived artifact; CPython
   transparently recompiles on the next import. This is what makes an
   aggressive GC safe, as the reporter's wipe confirmed.

## Fix (two independent halves)

- **Close the unbounded input** — `PYTHONPYCACHEPREFIX` joined `PYTHONPATH` /
  `PYTHONHOME` in `sandbox._PYTHON_ENV_PREFIXES`, the set scrubbed on the
  `strip_python_env=True` kiro-cli / agent spawn path (and only there:
  Kiro Crew's own sandboxed Python children — cron scripts, app backends —
  keep the prefix so the packaged app's bundle stays clean). Foreign
  interpreters in the agent subtree now write `__pycache__` beside their own
  sources, Python's normal behavior.

- **Bound what legitimately remains** — new `pycache_gc.prune_pycache`
  (mtime TTL `PYCACHE_MAX_AGE_DAYS` + oldest-first size cap
  `PYCACHE_MAX_TOTAL_BYTES`, `.pyc`-only, symlink-refusing, empty-dir
  pruning), invoked from `session.py`'s periodic maintenance sweep on the
  maintenance executor at most once per `PYCACHE_GC_INTERVAL_SECS`. The first
  sweep tick after gateway start also clears pre-existing multi-GB residue on
  installs that predate the scrub.

## Alternatives considered

- **Config keys (`cache_max_gb`, `cache_ttl_days`).** Not added: the cache is
  a pure derived artifact with no user-visible tuning need once the leak is
  closed; module-owned constants keep the limit where the code-style index
  can find it. A config surface can be added later without migration cost.
- **`kirocrew cache prune` CLI.** Unnecessary once GC is automatic; also
  avoids the MCP-first obligation a new LLM-facing CLI command carries.
- **Dropping `PYTHONPYCACHEPREFIX` in Electron.** Rejected — it would
  re-introduce the codesign-seal breakage the variable exists to prevent.
