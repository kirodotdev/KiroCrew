# Denied-Command Opt-Out → Keystone Trust-Root Storage — Implementation Plan

> **For agentic workers:** implement task-by-task; each task ends with tests green.

**Goal:** Move the denied-command opt-out state (`disable_all`/`disabled_ids`/`user_added`) out of the agent-readable `~/.kirocrew/config.json` and into a new keystone file `~/.kirocrew/denied_commands.json` on the `_SENSITIVE_HOME_DIRS` floor, so the agent cannot tamper with its own deny ceiling via ANY shell trick — then remove the fragile bash write-protection matcher that was chasing shell-obfuscation bypasses.

**Architecture:** The new file inherits the mature `is_sensitive_path` gate (read+write block; already handles variable-indirection, symlinks, KIROCREW_HOME, interpreters, casefold, realpath). Legitimate mutations go through the dashboard `/api/security/…` endpoints (out-of-band, not through the agent tool gate) — identical to how `security_policy.json` is operator-edited. No migration: the feature is unreleased, so no existing install has this state in config.json.

**Tech Stack:** Python 3.9+, `_atomic_json_write`, `_get_config_lock`, existing keystone patterns.

## Global Constraints
- Line length 100 (black); flake8 clean (F401/N806/W504); mypy clean.
- No emojis. `from __future__ import annotations`.
- The opt-out file is a security ceiling → full read+write block (`_SENSITIVE_HOME_DIRS`), NOT write-only.
- Keep `is_sensitive_write_path` (pre-existing helper, externally consumed by `_pathcheck.py`); only empty its config.json entries.

---

### Task 1: New keystone path + `_SENSITIVE_HOME_DIRS` entry

**Files:**
- Modify: `src/kiro_crew/config/loader.py` — add `denied_commands_path()` (mirror `config_path()`, KIROCREW_HOME-aware).
- Modify: `src/kiro_crew/security.py` — add `".kirocrew/denied_commands.json"` to `_SENSITIVE_HOME_DIRS`.
- Modify: `src/kiro_crew/platform/governance.py` — add the path to the boot-integrity `required` tuple (~line 1429).
- Test: `test/test_security.py` (is_sensitive_path covers it), `test/test_denied_commands_authority.py` or governance test (boot-integrity).

- [ ] Add `denied_commands_path() -> Path` returning `config_dir() / "denied_commands.json"`.
- [ ] Add the `.kirocrew/denied_commands.json` entry to `_SENSITIVE_HOME_DIRS` with a rationale comment (it holds the deny ceiling opt-out; agent cannot read/write its own ceiling).
- [ ] Add it to the governance boot-integrity `required` tuple so boot asserts it's floor-protected.
- [ ] Test: `is_sensitive_path("~/.kirocrew/denied_commands.json")` is True; a bash write AND read via `is_sensitive_bash_command` are both blocked; boot-integrity passes.

### Task 2: Read path — load opt-out state from the keystone file

**Files:**
- Modify: `src/kiro_crew/hooks.py` — new `load_denied_commands_state() -> dict` reads `denied_commands_path()` (fail-soft to `{}`); `effective_denied_regexes_from_config()` reads the new file, not config.json `hooks`.
- Modify: `src/kiro_crew/cli_server.py:936`, `src/kiro_crew/slack/gateway.py:1124` — feed `HooksConfig.from_dict` the merged dict (config.json `hooks` + the keystone `denied_commands` sub-object).

- [ ] `HooksConfig.from_dict` keeps reading `data["denied_commands"]`, but boot callers inject the keystone file's contents under that key (so `from_dict` is unchanged, callers merge).
- [ ] `effective_denied_regexes_from_config()` (cron vetting) reads `load_denied_commands_state()` instead of config.json `hooks.denied_commands`.
- [ ] Test: opt-out state written to the keystone file is honored by `HookManager`; config.json `hooks.denied_commands` is IGNORED (no longer a source).

### Task 3: Write path — mutations target the keystone file

**Files:**
- Modify: `src/kiro_crew/dashboard/handlers/security.py` — `_read_config_data`/`_read_config_strict`/`_denied_state`/`_write_denied_state` operate on `denied_commands_path()`; the file's whole content IS the `{disable_all,disabled_ids,user_added}` object (no `hooks.` nesting). `_reload_live_hooks` merges the keystone state into a fresh `HooksConfig`.

- [ ] Read helpers read `denied_commands_path()`; the JSON root IS the opt-out object.
- [ ] `_write_denied_state` atomic-writes `denied_commands_path()` (0600), under `_get_config_lock()`.
- [ ] The 6 mutations' `_mutate(denied)` operate on the root object.
- [ ] `_reload_live_hooks` rebuilds `HooksConfig` with the new keystone state merged over config.json `hooks`.
- [ ] Test: each endpoint round-trips through the keystone file; config.json untouched by mutations; sibling config.json keys never rewritten.

### Task 4: Remove the config.json bash write-protection apparatus

**Files:**
- Modify: `src/kiro_crew/security.py` — remove `_build_write_protected_re`, `_get_write_protected_res`, `_bash_writes_protected_config`, `_WRITE_INDICATOR_RE`, the `is_sensitive_bash_command` call to it; empty `_WRITE_PROTECTED_HOME_PATHS` config.json entries (keep the list + `is_sensitive_write_path` for the external `_pathcheck.py` consumer).
- Modify: `src/kiro_crew/hooks.py` — remove the edit-tool gate lines 515-520 (config.json write block) IF no longer needed (config.json no longer holds ceiling state; resource-ceiling clamp already handles bounds).
- Delete: `test/test_security.py::TestWriteProtectedConfigBash`.

- [ ] Remove the bash matcher functions + the call site + `_WRITE_INDICATOR_RE`.
- [ ] Empty the config.json entries from `_WRITE_PROTECTED_HOME_PATHS` (leave `[]` with a comment, or remove the two entries) — keep the constant + `is_sensitive_write_path` helper.
- [ ] Remove the hooks.py edit-tool config.json write gate (now redundant).
- [ ] Delete `TestWriteProtectedConfigBash` (the vectors are moot — the file no longer holds ceiling state, and the keystone file is covered by `is_sensitive_path`).
- [ ] Test: full security suite green; `is_sensitive_bash_command` no longer references write-protected config.

### Task 5: Update test fixtures + specs + AutoSDE rules

**Files:**
- Modify: `test/test_denied_commands_api.py`, `test/test_denied_commands_hooks.py`, `test/test_heartbeat_safe_tools.py` — fixtures write the keystone file, not config.json `hooks.denied_commands`.
- Modify: `docs/system-specs/modules/security.md`, `governance.md`, `docs/security-deep-dive.md` — describe keystone storage; remove the write-protected-config-bash narrative.
- Modify: `.github/workflows/code-review.yml` — replace the config.json-write-protection tripwire with a keystone-storage tripwire.

- [ ] Update all test fixtures to the new storage location.
- [ ] Update specs to the keystone model; drop the bash-matcher description.
- [ ] Update AutoSDE tripwires (config.json write-protection → keystone-floor coverage of denied_commands.json).
- [ ] Full quality gate: black, isort, flake8, mypy, pytest (affected suites) green.
