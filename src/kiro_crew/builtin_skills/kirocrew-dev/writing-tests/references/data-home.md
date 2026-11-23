# 1b. The operator's data home, and `~/.kiro`

Moved verbatim from the writing-tests `SKILL.md` core (Rule 1b).

`KIROCREW_HOME` is pinned per test at the rootdir, which is what makes `config_dir()`
safe — and it needs to be, because resolving it is **not a read**: it creates the home
and its marker, and can run the `~/.kirocrew` → `~/.kiro/crew` migration.

Six shapes escape the env var:

- **`monkeypatch.undo()` in the body of a test.** It reverts EVERY record on the shared
  instance — your own patches, every fixture that set something through it (`stores`
  pins `KIROCREW_HOME` that way), and until the floor got its own instance, the floor
  itself. A restore that ran `empty_trash()` after `undo()` emptied the operator's real
  trash. A patch you need to drop before the test ends goes in
  `with pytest.MonkeyPatch.context() as patched:`; `undo()` is not a scoped tool.
- **A detached task.** `asyncio.create_task(...)` at boot with no one awaiting it keeps
  running after the test that started it has returned; when its work resolves a path
  (`peek_next` → `spool_path()` → `data_home()`, on a `to_thread` worker) it resolves
  the OPERATOR's home. Fixed where the task is scheduled: `_start_channel_transports`
  resolves `spool_path()` on the loop and hands it to the task, so the worker reads a
  location fixed at boot. Resolve every environment-derived input of a detached task on
  the calling side before detaching, or give the test a handle it can await.
- **What a closed loop leaves behind.** pytest-asyncio 0.21 ends the loop with a bare
  `loop.close()`. A pending task's coroutine then gets `GeneratorExit` at garbage
  collection, so its `finally` blocks run *then* (`_run_chat`'s queue cycle reaches a
  sync `KiroCrewConfig.load()`); a default-executor job (`to_thread`) is abandoned
  mid-flight. Both resolved `config_dir()` after the unpin and grew a fresh fake
  `HOME`'s `~/.kiro/crew`. The floor's `tryfirst` teardown hook now cancels pending
  tasks, runs them to completion and joins the executor while the pins hold, the way
  `asyncio.run` shuts down. A `coroutine ... was never awaited` warning pointing at that
  hook means your test left an unstarted turn behind: await it, or keep the patch that
  was meant to catch it in force until it has run.
- **A default that bypasses the pin on purpose.** `PodConfig.load()` derives the pod
  plane from `_default_home()` / `Path.home()` so a pod cannot redirect the host's
  registry; the floor pins `KIROCREW_POD_ROOT` / `KIROCREW_POD_ENV_DIR` for that reason.
  A new resolver with its own default gets a floor pin AND a ratchet in
  `test_host_isolation_floor.py` in the same change.
- **Import-time from `config_dir()`** (`subagent_persistence._SUBAGENTS_DIR`): the var
  is read after the module captured the path. Each has its own autouse pin. A
  **collection-time probe** is the same shape one step earlier: a module-level
  probe used by a `skipif` (`_sandbox_can_spawn()` in `test/test_app_backend.py`)
  runs before any pin and would read the REAL `config.json`; run such probes under
  an empty `KIROCREW_HOME`.
- **Import-time from `Path.home()`**: `~/.kiro` is *kiro-cli's* home, machine-wide and
  shared with the real installed agent — `~/.kiro/settings/mcp.json` is the live
  agent's MCP server list. `_isolate_shared_kiro_paths` redirects these from a table,
  and `test/test_host_isolation_floor.py` **fails when `src/` grows a new one**. That
  ratchet covers IMPORT-TIME bindings only. The lazy resolver
  `config.paths.kiro_home()` — and so `kiro_agents_dir()` / `kiro_sessions_dir()` — is
  yours to isolate, with `KIRO_HOME` or by patching `Path.home()`; they are not
  interchangeable, because the env var outranks the other. A test that skipped this
  projected the developer's *installed* agent specs and failed with an
  `AcpRuntimeError` naming a prompt file in an unrelated worktree.

  Some entries are excluded, and for two opposite reasons — already redirected
  elsewhere (the macOS launchd set, moved as one group by `_isolate_launchd_paths`),
  or a **security anchor that must never move**: the file browser's allow-list root
  `file_explorer/server._HOME`. **Stub the reader, never
  move the anchor.** Redirecting a matcher so a test can pass makes it assert against a
  pattern that no longer matches the thing it protects. An anchor that stops being an
  import-time `Path.home()` binding leaves this tripwire's reach entirely —
  `kiro_usage_api`'s kiro-cli sqlite tuples now resolve the home inside
  `identity_stores.sqlite_dbs()`, so their anchor rule is pinned by
  `test_identity_stores.py::TestUsageTuplesAnchorTheRealHome` instead. Read
  `_EXCLUDED` for the live list rather than a copy here.

**A faked host condition and the check that reads it are one contract.** A test
that fakes the default home (`beacon.is_default_home`), the platform or a
privilege depends on the production check it answers. Change the fake and the
check in the same pull request: a new check means searching `test/` for the
tests that reach it, and removing a fake means searching for every assertion that
still relies on it. Otherwise one pull request that drops a fixture's fake and
another that adds a check reading it are each green alone and red together on
`main`. Keep the fake scoped to the class that needs it. Full rule:
[testing-conventions.md](../../../../../../docs/system-specs/common/testing-conventions.md#a-faked-host-condition-changes-with-the-check-that-reads-it).
