# Code style: constants, comments, lint

## No hardcoded values in business logic

Every limit, timeout, protocol string and user-facing label has ONE owning
module. A literal inlined at a call site cannot be found, tuned, or tested, and
the second copy of it drifts silently.

Before adding a constant, check the table for an existing owner. Module-level
constants are `UPPER_SNAKE_CASE`; private ones are `_UPPER_SNAKE_CASE`.

Paths below are relative to `src/kiro_crew/`.

| Concern | Owning module | Notes |
|---|---|---|
| ACP protocol strings | `acp/types.py` | `EVENT_*` event kinds and `METHOD_*` JSON-RPC method names. |
| Provider event kinds | `providers/base.py` | Re-exports the `EVENT_*` names from `acp/types.py`; that re-export is the seam providers code against, not a second definition. |
| ACP client timeouts | `acp/client_timeouts.py` | `_INIT_TIMEOUT`, `_DEFAULT_PROMPT_TIMEOUT`, `_READ_TIMEOUT`, `_STALE_TURN_TIMEOUT`, `_TOOL_STALL_TIMEOUT`, `_WAIT_RESPONSE_MAX_TIMEOUT`, `_SEL_AUDIT_TIMEOUT_SECONDS`. |
| MCP protocol version | `mcp_shared_dispatch.py`; read as `mcp_shared` | The `initialize` reply's `protocolVersion` for the managed stdio servers. `mcp_cron.py` / `mcp_core.py` do not carry their own copy. |
| Credential keys | `config/loader_credentials.py` | `CRED_*` env-var names plus the `CREDENTIAL_KEYS` tuple. |
| Dashboard port | `config/loader_credentials.py` | `_DEFAULT_PORT` (5476) and `DASHBOARD_PORT` (honors `KIROCREW_PORT`). `dashboard/origin.py` imports `_DEFAULT_PORT` rather than restating it. |
| Hook results and event names | `hooks.py` | `HOOK_PASSTHROUGH` / `HOOK_REPLY` / `HOOK_MODIFY` / `HOOK_INJECT_CONTEXT`, the tool verdicts `TOOL_ALLOW` / `TOOL_AUTO_APPROVE` / `TOOL_DENY`, and `HOOK_EVENT_*` / `HOOK_EVENTS`. |
| Memory paths and dir names | `memory.py` | `WORKSPACE_DIR_NAME`, `MEMORY_DIR_NAME`, `HISTORY_DIR_NAME`, `PREFERENCES_FILE`, `PROJECTS_FILE`, `INDEX_DB_FILE` (the FTS index *name* only — which directory a given store's index sits in is store policy, owned by `memory_stores.memory_index_path_for`). |
| Named memory stores | `memory_stores.py` | `MEMORY_STORES_DIR_NAME`, `DEFAULT_MEMORY_STORE`, `MEMORY_DB_FILE` (the vector-store filename, which `vector_memory._DB_FILE` aliases rather than restating), `MEMORY_STORE_NAME_MAX`, `_STORE_NAME_RE`, `_WINDOWS_RESERVED_BASENAMES`; the tree's host-local entries `MEMBER_API_KEY_FILE`, `MEMBER_BACKUPS_DIR_NAME`, `EXECUTION_LOGS_DIR_NAME`, `STORE_BACKUP_DIR_NAME` (which `memory_backup.BACKUP_DIR_NAME` aliases), and the layout predicates `is_host_local_store_state` / `named_store_product_file` the snapshot and export read. A stdlib-only LEAF module, so `security.py` can build its sensitive-path fence from `MEMORY_STORES_DIR_NAME` without a cycle. |
| Lesson limits | `learn.py` | `_LESSONS_FILE`, `_MAX_LESSONS_TOTAL` (prune oldest past the total). |
| Cron limits | `cron_service/store.py`, `fields.py`, `model.py`, `schedule.py`; the reaper intervals in `cron.py` | `_CRONS_FILE`, `_STORE_VERSION`, `_MIN_INTERVAL_SECS`, `_JOB_TIMEOUT_SECS`, `_TIMER_POLL_SECS`, `_AUTO_PAUSE_THRESHOLD`, the reaper intervals, the skip-date horizon, the store file-lock timeouts, and the hourly/daily jitter caps and the jitter wait's wall-clock slice. |
| Session and transcript limits | `history.py` and its owners | `SESSIONS_DIR_NAME`, `ARCHIVE_RETENTION_DAYS` (`history_files.py`), the JSONL rotation pair `_SESSION_MAX_BYTES` (10 MB) / `_SESSION_KEEP_LINES` (`history.py`), the file-lock timeouts (`history_locks.py`), and the search caps (`SEARCH_MIN_CHARS`, `_SEARCH_SCAN_WINDOW`, `_TITLE_BOOST`, in `history_search.py`). |
| Context budgets | `context_assembly/budget.py` | `_CONTEXT_BUDGET_BASE` plus one `_budget(fraction)` cap per block (history, preferences, projects, lessons, semantic, episodic, skills, steering, compressed history, preamble headroom). Budgets are expressed as FRACTIONS of the base, so read them there rather than quoting a byte figure. |
| Task states | `task.py` | The `TaskState` enum, the `_TERMINAL` set, and the `_TRANSITIONS` map that defines the legal state machine. |
| Heartbeat intervals | `heartbeat.py` | `_DEFAULT_INTERVAL`, `_FTS_REBUILD_TICKS`, `_PRUNE_TICKS`, `HEARTBEAT_TASK_TIMEOUT_SECS`, `HEARTBEAT_FILE`. |
| Subagent limits | `subagent_parts/run_limits.py` (`_MAX_DONE_RESULT_LEN` in `subagent_parts/run_output.py`); read and patched as `subagent.<name>` | `_MAX_CONCURRENT`, `_TIMEOUT_SECS`, `_TURN_LIMIT`, `_MAX_DONE_RESULT_LEN`, `_STARTUP_TIMEOUT_SECS`, `INJECTION_TIMEOUT`, the reaper/stall intervals. |
| Slot state caps | `dashboard/state.py` and its owners in `dashboard/state_parts/` | `_MAX_SLOT_MESSAGES`, `_MAX_PERSISTED_NOTIFICATIONS`, `_MAX_SOURCE_LINKS_PER_SLOT`, `_MAX_PENDING_CONTEXT`, `NATIVE_SUBAGENT_DONE_RESULT_CAP`, `_QUESTION_TIMEOUT_MAX`. |
| Injected-message envelope prefixes | `constants.py`, `dashboard/state_parts/injected_turns.py` | `constants.py` owns `SUBAGENT_COMPLETION_PREFIX` / `SUBAGENT_BATCH_COMPLETION_PREFIX`; `dashboard/state_parts/injected_turns.py` (re-exported by `dashboard/state.py`) owns the cron, monitor, synthesis, and `*_RECOVERY_PREFIX` markers and aggregates `SUBAGENT_COMPLETION_PREFIXES`. See [injected-messages](injected-messages.md). |
| Usage cache TTLs | `dashboard/handlers/usage_sessions.py`, `usage_history.py`, `usage_context.py` (served by `dashboard/handlers/usage.py`) | `_CACHE_TTL`, `_TOKEN_CACHE_TTL`, `_CONTEXT_CACHE_TTL`, `_TOKEN_HISTORY_DAYS`, `_CONTEXT_TOP_SESSIONS`. |
| Webhook hook limits | `dashboard/handlers/hooks_webhook_intake.py` (served by `dashboard/handlers/hooks.py`) | `_HOOK_MAX_CONCURRENT` (semaphore-backed, 429 past it), `_HOOK_MESSAGE_MAX_LEN`, `_HOOK_TIMEOUT_DEFAULT` / `_HOOK_TIMEOUT_MAX` (both prime, to avoid a thundering herd with cron intervals). |
| Embed cache | `embeddings_shared.py`; read and patched as `embeddings.<name>` | `_EMBED_CACHE_MAX` (128 entries, keyed by text plus model id; the comment there carries the memory arithmetic). |
| Bytecode-cache GC limits | `pycache_gc.py` | `PYCACHE_MAX_AGE_DAYS`, `PYCACHE_MAX_TOTAL_BYTES`, `PYCACHE_GC_INTERVAL_SECS` (the `<data home>/cache/pycache` TTL, size cap, and periodic-sweep cadence). |
| Shell audit log cap | `shell_audit_log.py` | `SHELL_AUDIT_LOG_NAME`, `SHELL_AUDIT_LOG_MAX_BYTES` (the file the bundled `postToolUse` hook appends to, and the size at which the session cleanup loop rotates it aside to one `.1` generation). |
| Slack UX strings and pacing | `slack/handler.py` | `_THINKING`, `_CURSOR`, `_NO_RESPONSE`, `_STATUS_WORKING`, `_TRUNCATION_MARKER`, plus `_EDIT_INTERVAL`, `_APPROVAL_TIMEOUT`, `_SLACK_SECTION_TEXT_LIMIT`, the stall thresholds and the phase debounce. |
| Cross-cutting shared constants | `constants.py` | `KIROCREW_SPAWNED_ENV`, `ENV_TRUTHY`, `CHAT_TURN_TIMEOUT`, `COMPACT_WAIT_TIMEOUT_SECS` (one budget, shared by manual and automatic compaction; `session.compact_wait_secs` raises it for both, resolved for every caller by `SessionManager.compact_wait_budget_secs()`), the `[OPTIONS:]` parse regexes, `BANNER`, `MAX_BANNER_CHARS` and `ARTIFACT_MAX_CONTENT_BYTES` (bounds `validation.py` — a leaf — must read without importing the service module that enforces them; `artifacts.MAX_CONTENT_BYTES` re-exports the latter). |
| Coder execution | `coder/client.py`, `coder/registry.py`, `constants.py` | CLI deadlines and output bounds belong to the client. Generated workspace-name bounds belong to the registry; `CODER_WORKSPACE_PREFIX_MAX_CHARS` lives in `constants.py`. |
| Gateway shutdown budget | `gateway_shutdown_budget.py` | Gateway cooperative timeout, service-manager signal margin, and the derived systemd/launchd stop deadline. |
| Process-wide shutdown signal | `__init__.py` | `shutdown_event`. Background loops `await shutdown_event.wait()` with a timeout instead of a plain `asyncio.sleep`, so they wake instantly on Ctrl-C. |
| Base agent config | `config/defaults.json` | `tools`, `allowedTools`, `resources`, `hooks`, model. Packaged as package data, so editing it needs no code change. |
| Managed MCP server specs | `agent.py` | `_MANAGED_MCP_SERVERS`: which servers are auto-registered and refreshed while preserving user customizations. |
| AgentCore policy-field validators | `platform/agentcore_schema.py` | `AGENTCORE_GATEWAY_URL_MAX`, `WORKLOAD_NAME_MIN` / `WORKLOAD_NAME_MAX`, `normalize_agentcore_gateway_url`, `normalize_agentcore_workload_name`. AWS-free so governance can parse a policy without the optional extra. |
| Kiro relay protocol v1 | `relay_client/protocol.py` | Every frame size, timeout, close code and error code of the relay's host leg (`PING_INTERVAL_SECONDS`, `SILENCE_REPLACE_SECONDS`, `BACKOFF_STEPS_SECONDS`, `MAX_TEXT_FRAME_BYTES`, `CLOSE_*`, `ERR_*`). `test/test_relay_client_protocol.py` pins them to the copied contract fixtures. The relay's provisional names (`HOST_URL_TEMPLATE`, `CREW_DOMAIN_TEMPLATE`) are settings defaults in `relay_client/provider.py`. |
| Built-in skills | `builtin_skills/<name>/SKILL.md` | Frontmatter (`always`, `triggers`, `dir`) is the skill's own contract. This is the only tree copied into a user's `~/.kiro/crew/skills/`, so a skill any shipped feature, tool or doc references MUST live here. The top-level `skills/` tree is repo-checkout-only and reaches no installed user. |

Other style rules:

| Rule | Requirement |
|---|---|
| Line length | 100 chars (black and isort are both configured to it) |
| Python version | >= 3.12; `from __future__ import annotations` for type hints |
| Imports | `import logging` plus `logger = logging.getLogger(__name__)`; a split's owner names the original module instead ([Splitting a file](#splitting-a-file)) |
| Async | `asyncio` throughout; `async def` for all I/O |
| Module-global asyncio primitives | Never a bare `asyncio.Lock()`/`Event()`/`Queue()` at module scope — it binds to the import-time (or first-use) loop and raises `RuntimeError` from any other loop (Python 3.10+). Use `kiro_crew.loop_lock.LoopBoundLock` for locks, or create the primitive inside the coroutine. CI enforces this (`loop-bound-locks` gate). |
| Dataclasses | `@dataclass` for data containers |
| Product name | The product is **Kiro Crew**: two words, a space, capital `K`. Identifiers keep the spelling their own system gave them (the `kirodotdev/KiroCrew` repo slug, `KiroCrew.dmg` artifacts, the `KiroCrew Nightly` OS identifier, the `kirocrew` CLI, `KIROCREW_*` env vars, `kiro_crew` imports). CI gates the lines a change ADDS, so an existing spelling nearby does not exempt a new one; run `BRAND_BASE_REF=origin/main python3 scripts/check_brand_name.py` before pushing. |
| Errors | ACP exceptions live in `acp/transport_errors.py` (re-exported by `acp/client.py`), `acp/session_handle_runtime_contract.py` (re-exported by `acp/session_handle.py`), and `acp/runtime.py` (the binding and session-start errors in `acp/runtime_session_setup.py` and `acp/runtime_session_create.py`, re-exported by it); return error strings at tool boundaries. See [error-handling](error-handling.md). |

## Comments explain the WHY

A comment carries what the code cannot: invariants, edge cases, units,
non-obvious constraints, and the reason a surprising choice is correct. It is not
a restatement of the line below it, and it is not a task log.

Do NOT put in a comment or docstring:

- PR or review numbers, review-round or finding markers, ticket ids
- incident dates, milestone tags, commit SHAs
- historical narration: "previously", "used to", "we now", "no longer",
  "historically", "Status: implemented"
- a change's place in a sequence of changes: "hotfix", "follow-up to",
  "regression for", "round N", "GPT round", "review round"

That history lives in git. State CURRENT behavior in present tense. A comment that
narrates a change is stale the moment the next change lands, and a reader cannot
tell whether it describes the code in front of them.

Keep them concise. `_vendor/` (vendored third-party code) and pragma comments
(`# type: ignore`, `# noqa`) are exempt.

`scripts/check_comment_history.py` enforces this, and the list above IS its rule
set: every phrase named there is a pattern in the script, and the script matches
nothing the list does not name. It reads only comment tokens and docstrings, so a
string literal that is not a docstring is never scanned and a user-facing message
using one of these phrases is fine. Present-tense purpose is not narration:
"regression test pins this shape" passes, "regression for the truncated parse"
does not.

The gate is diff-scoped, like the brand-name gate: it judges only the lines a
change ADDS, measured against `COMMENT_HISTORY_BASE_REF` (the PR's base in CI).
Added lines are complete for regression — a line only reaches `main` through a
diff that added it — and they are the only lines a contributor can act on. There
is no baseline file: a shared per-file count would make one JSON the merge-conflict
hotspot of every cleanup PR. The legacy markers the tree still carries are not
tracked; a marker can re-enter only on an added line, which is what the gate
judges. Without the env the script prints whole-tree counts and does not enforce.

The rule applies to every comment. The gate enforces it only on added lines of
`.py` files under `src/kiro_crew` and `test`; comments elsewhere (`website/`, the
Electron shell, workflow files) are held to the rule by review.

```bash
COMMENT_HISTORY_BASE_REF=origin/main python3 scripts/check_comment_history.py
```

## The lint pitfalls

The blocking gates are black (baselined), the subprocess-encoding gate (baselined), the comment-history gate (diff-scoped), isort, flake8 and mypy. Run them before
committing:

```bash
python3 scripts/check_black_formatting.py && python3 scripts/check_subprocess_encoding.py
COMMENT_HISTORY_BASE_REF=origin/main python3 scripts/check_comment_history.py && isort src/kiro_crew test conftest.py xdist_budget.py
flake8 src/kiro_crew test conftest.py xdist_budget.py && mypy src/kiro_crew
python3 scripts/local-gate.py
```

The last line runs the tests related to your diff, not the full suite -- that is
CI's job (`local-gate.py --full` exists for a human who wants it locally).

`black --check` cannot be run bare: 1,420 files under `src/` and `test/` predate
any enforcement, so a repo-wide run reformats ~95,800 lines. Those files are
recorded in `.github/black-baseline.txt` and exempted; every other file must be
clean, and a file that *becomes* clean must be pruned from the list, so it only
ever shrinks. Prune with `python3 scripts/check_black_formatting.py
--update-baseline`; it refuses unless the installed black equals the
`pyproject.toml` pin (`black==26.10.1`). The read-only check does not need the pin. Format what you touched with
`black --target-version py310 <paths>`, never the whole tree.

**On macOS, run `mypy --platform linux src/kiro_crew`.** CI type-checks on Linux, and
typeshed guards `os.listxattr` / `getxattr` / `setxattr` behind
`sys.platform == "linux"` even though macOS has them. A bare local run therefore
reports errors in files you did not touch, and — the half that matters — it MISSES the
Linux-only errors CI fails on, so a clean local run is a false green.

| Gate | Rule | Detail |
|---|---|---|
| flake8 F401 | No unused imports. | Remove any import not directly used in the file. A pyflakes check, on everywhere except the split facades listed under `setup.cfg`'s `per-file-ignores`, each row under a comment naming the test that stands in for it ([Splitting a file](#splitting-a-file)). |
| flake8 N806 | Function-local variables are lowercase: `mock_client`, not `MockClient`. | Needs the `pep8-naming` plugin in the flake8 environment. The repo's pinned `flake8==7.1.0` alone does not report it, so an N806 that passes locally can still be flagged by a reviewer or a differently-provisioned environment. In-function Windows API constants carry `# noqa: N806`. |
| flake8 W504 | Line break BEFORE a binary operator, not after. | Both W503 and W504 are in pycodestyle's DEFAULT_IGNORE, and `setup.cfg` uses `extend-ignore` (not bare `ignore`) precisely so those black-compatible defaults stay in effect. So neither fires in a normal run; W503 is additionally listed explicitly because black formats that way. Write it operator-first anyway, because that is what black produces and what the codebase reads like. |
| mypy | Annotate empty collections: `output: list[str] = []`. | `check_untyped_defs = true`, so this fires inside unannotated functions too. mypy only needs the annotation when it cannot infer the element type from the same scope, which is why an `out = []` that is immediately `append`ed and returned from an annotated function is fine. |
| pytest | `asyncio: mode=strict`, so every async test needs `@pytest.mark.asyncio`. | Without the marker the coroutine is collected but never awaited, and the test passes having run nothing. |

`setup.cfg` also sets `max_line_length = 100`, ignores E501 (after auto-formatting
the only long lines left do not matter) and E203 (black's whitespace before `:`),
and excludes `src/kiro_crew/_vendor` from flake8 entirely. mypy and isort exclude
it too. Never hand-edit vendored code, and never reformat it: the point is a clean
diff against the upstream wheel at upgrade time.

The formatter, linter and type-checker are pinned to exact versions in both
`setup.cfg`'s `dev` extra and `pyproject.toml`'s `dependency-groups`, because
black and mypy change their output across minor releases and a floating range
makes a local venv disagree with CI. Bump them in lockstep.

## No file over 2,000 lines

A hand-written code file holds at most 2,000 lines: Python, TypeScript and
JavaScript, CSS, shell, PowerShell and Swift, backend and frontend alike. Put new
code in a new module. When a file nears the cap, move whole definitions into a
sibling module and keep the original path as the import path callers use. A name
tests patch must stay patchable there, through a forwarding facade or call-time
reads of the facade (see
[testing-conventions](testing-conventions.md#patch-the-defining-module-not-a-re-export)):
a plain re-export silently stops the patch reaching the moved code.

`scripts/check_file_line_cap.py` checks every tracked code file, and
`test/test_file_line_cap.py` runs it in the backend test suite (Linux and
Windows) on every change, including the leaf-test and frontend-only reductions,
and pins every rule. Only tracked files count, so `git add` a new file before
checking it locally.
Generated output, vendored code, data, prose and workflow YAML are exempt; the
script's `EXEMPTIONS` table names each one with its reason. A line counts the way
`wc -l` counts it, plus one for an unterminated last line.

The files that were already over the cap are listed in
`.github/file-line-cap-baseline.txt` as `<lines> <path>` rows, and the list only
shrinks. The tree is scanned whole, but the verdict is scoped to the change
(`scripts/ratchet_scope.py`, as for the black and subprocess-encoding gates,
read with `--no-renames` so a renamed file's old path counts): a violation in a
file the change does not touch is main drift, a warning, because two changes
that were each green can together push a file over the cap.

| Situation | Verdict |
|---|---|
| A file over the cap that the baseline does not list | Fails if the change touches it; split it, never add a row. Otherwise a main-drift warning. |
| A listed file now within the cap, deleted, or exempt | Fails if the change touches it, until its row is deleted in the same commit as the split. Otherwise a main-drift warning. |
| A baseline edit that adds a path, raises a count, or lowers a row below its file's size | Fails: the baseline only shrinks, and a row it writes is never below the file's measured size nor above the row at the base. A deletion-only or lowering edit is judged by the rows it changed, so someone else's stale row beside it stays a main-drift warning. |
| A listed file that grew past its row | Reported, not failed. It must not grow; split it. |
| A malformed or unsorted baseline | Fails. |

Backend Lint and Frontend Lint run the script on the full history and enforce
the scoped verdict, and the main ratchet audit judges the whole tree, so main
drift turns red there. The test runs on the backend shards' shallow checkout,
which cannot tell which files a change touched, so there it reports violations
without failing.

After a split, delete your own row in the same commit. Delete only your own
rows: never edit someone else's, by hand or with `--update-baseline`. The
refresh edits only the rows of files your change touches; it deletes stale rows
and lowers counts, and never adds a path or raises a count. Where it cannot tell
which files the change touches, it refuses unless `--whole-tree` asks it to
rewrite every row:

```bash
python3 scripts/check_file_line_cap.py --update-baseline
```

### Splitting a file

A new responsibility gets a new module, never a new section in a large file, and a change
that takes a code file past 1,600 lines splits it in the same PR or in one before it.
Every module a split creates holds 1,600 lines or fewer, so the next change has room, and
the split deletes or lowers its own baseline row as described
[above](#no-file-over-2000-lines). Split by responsibility, never by line ranges: each new
module owns one job you can name in a sentence and is named for it, as a sibling
`<module>_<topic>.py` or a module in a `<module>_parts/` package (with an `__init__.py`,
or `packages = find:` leaves it out of the wheel).

**The original module stays the import path**, as a facade over its owners built with
`src/kiro_crew/_facade_mirror.py` in the shape its docstring shows: the foot imports every
owner, lowest layer first, calls `install()` and sets the module's `__class__` to
`MirroredModule`, so a write through the facade (a test's `monkeypatch.setattr` or
`mock.patch` on the old path) lands in every owner that binds the name. A plain re-export
copies the binding instead, and the patch silently stops reaching the moved code; see
[testing-conventions](testing-conventions.md#patch-the-defining-module-not-a-re-export).
`__all__` keeps the list the module had, or is derived from the namespace where it had
none. A family that already composes through `src/kiro_crew/_facade_compose.py`
(`hook_runtime/`, `dashboard/chat_api/`, `dashboard/server_runtime/` and the other owner
packages) takes new owners inside its package; start a new compose family only when moved
functions must run on the facade's own globals, as when a contract pins their `__module__`.

In a mirror family, moved code reads its names as plain globals of its own owner, and the
family keeps two rules:

- an owner imports its facade only under `if TYPE_CHECKING:`, and imports only the owners
  listed before it;
- a global that code rebinds at run time (`global name; name = ...`) lives in one owner
  with every function that rebinds it and every reader, since no mirror can follow a
  rebinding. The facade binds no copy, so the mirror serves it live from that owner (a
  `surface=` facade lists it, as `_SERVED_LIVE` in `src/kiro_crew/dashboard/state.py`
  does). A reader that cannot live there looks the facade up in `sys.modules` at call
  time, never by importing it (`_facade()` in `src/kiro_crew/snapshot_components.py` shows
  the lookup, for the unmirrored `kiro_crew.snapshot`).

`test/test_facade_mirror_guards.py` finds every mirror facade on its own, refuses an owner
that imports its facade or rebinds a shared global, fails a facade no test patches through,
and executes only a few patches per facade. Each facade also gets a patch-reach test that
executes every name the test trees patch through it (read with `test/facade_mirror_scan.py`)
and proves the scan finds known seams (`test/test_session_pid_facade.py`). A check whose
body is the same for every facade (the known-seam scan, the corpus-gate derivation, the
layering, manifest and logger scans) is written once in `test/test_facade_shared_checks.py`
and parametrized per module: a facade test file names the ones it needs in a module-level
`SHARED_FACADE_CHECKS` tuple instead of copying their bodies, and keeps only the checks
particular to its facade.

**A large class keeps one class body**, in the facade or in one home module that sets
`__module__` to the facade when a pickle, repr or test reads it. Members move to owners as
functions taking `self`, and the body binds each as a one-line attribute
(`prompt = _sh_turn.prompt`, or `staticmethod(...)`) under a comment naming the owner,
with no new mixin or base class (`src/kiro_crew/acp/session_handle_class.py`). A member
that uses zero-argument `super()`, `__class__` or a name-mangled attribute stays in the
body. Keep `__module__` and `__qualname__` wherever anything reads them: moved exception
classes keep the facade's name because error text renders it, and the shared helper
`kiro_crew._facade_bindings.own_methods(cls, owners)` renames each function the class binds
from a module in its explicit owner set `<Class>.<name>`. An owner logs under
the original module's logger, `logging.getLogger("kiro_crew.<original>")`: the log format
prints the logger name, and `caplog` tests filter on it.

**Registries keyed by path follow the code.** A `_REDACTION_SINKS` row (in
`src/kiro_crew/security_posture_sinks_*.py`) names the module that redacts that sink; when a split
moves the redactor calls, re-key the row to the owner that now makes them. The sink test
in `test/test_security_posture.py` also counts calls in the facade's declared owners, so a
row left on the facade can stay green while it no longer tells an operator where the sink
is redacted; older splits did that (`dashboard/handlers/files.py`), a new one does not.
Every module the omission test's text scan matches (a word containing `redact`, or
`StreamRedactor`, followed by `(`, comments included), facade or owner, is a sink row or
is listed in `NON_EGRESS_REDACTION_MODULES` under a comment giving the reason. Every other
list keyed by a path or a module name gets truthful rows in the same commit.

**A new facade or binding test pins identity and reach, never an exact namespace or member
list.** Each public name the module defined, or imported from `kiro_crew`, before the split
keeps resolving on it, to its home module's object where the facade binds it by import:
`test/campaign_facade_inventories/<dotted.module>.json` records them for
`test/test_campaign_facades_base_public_names.py`. Pin names the split adds as a subset
(`added <= names`): the `set(vars(m)) - BASE_NAMES == {…}` and `present ^ set(_MEMBERS)`
freezes some older tests carry fail the next unrelated change that adds a name, and two
such changes that each passed can merge into a red main. A facade's re-exports carry
`# noqa: F401`, or it takes a per-file `F401` row in `setup.cfg` with a test that accounts
for every import (bound before the split, mirror glue, defined by an owner, or read by the
family's own code) and a planted import that proves the test can fail.

**Test files split by topic** into `test_<module>_<topic>.py` beside the original, with the
fixtures and helpers the parts share in `<module>_helpers.py` (no `test_` prefix, so pytest
does not collect it); basenames stay unique across `test/`, which is not a package.

- Test bodies move verbatim: the flake-pattern ratchet forgives a site only when its
  enclosing function moved unchanged. A `Test*` class bound at module level in another
  module is collected there a second time.
- An autouse fixture defined in a test or helpers module applies only there and where it
  is imported by name, so each part imports the ones its tests relied on. Copy the
  module-level `pytestmark`, `importorskip` and conftest flags (`ENFORCE_LAUNCH_APPROVAL`)
  into every part, and add each part wherever a conftest names the original module
  (`_HOST_SERVICE_EXEC_ALLOWED_MODULES` in `test/conftest_floor_services.py`,
  `_REAL_MCP_POST_MODULES` in `test/conftest.py`).
- Node ids move with their tests: rewrite their rows in the `test/` lists
  `windows-expected-failures.txt`, `macos-expected-failures.txt`,
  `codebuild-expected-failures.txt` and `requires-real-symlinks.txt` (nothing audits the
  macOS or symlink list for dead rows, so a stale row there silently drops its xfail or
  skip), list every part of a file `windows-collect-ignore.txt` or
  `macos-collect-ignore.txt` names, and split the file's `wait_discipline_baseline.txt` row
  so the parts sum to the old count.
- Prove nothing was lost: the node ids `pytest --collect-only -qq -n0` prints (addopts
  adds `--verbose`) are the same multiset before and after, up to the file name.

**Citations move with the code.** A citation of the file that defines moved code is
re-pointed to the owner in the same commit. Cite a symbol, not a line number: a split
moves every line, and the docs lint refuses a new backticked line-number citation and
fails one the split pushes past the end of the file.

**Frontend facades** re-export with explicit lists, `export { … } from './Owner'` and
`export type { … } from`, never `export *`: a star re-export publishes every owner export
the module never offered, and it never carries a default export. No owner imports its
facade, and no production code outside the family imports an owner unless the conventions
record the exception, because a spec that mocks the facade by its module id would hand
that code the mock or miss it; see
[frontend conventions](../../../website/docs/frontend-conventions.md#the-file-panel-the-chat-pane-and-the-browser-panel).

## Subprocess output is decoded explicitly

A text-mode subprocess call (`text=True` / `universal_newlines=True`) without an
explicit `encoding=` decodes the child's output with the locale's code page —
UTF-8 on POSIX, the legacy ANSI code page on Windows, where any non-ASCII byte
becomes mojibake (#3219). CI gates this with
`scripts/check_subprocess_encoding.py` (AST-based, so multi-line calls are
judged as one call), behind the shrink-only
`.github/subprocess-encoding-baseline.txt`.

For a child whose output encoding is knowable — `git`, `gh`, a Python
interpreter we spawn running our own code — pin the decode with the shared
definition in `kiro_crew.subprocess_utf8`: splat `**UTF8_TEXT` into the call
(this keeps the call going through the module's own `subprocess` attribute, so
tests that patch it by name keep intercepting — and it adds no new spawn
primitive for `test_spawn_audit` to police). For a Python child, also pin the
EMIT side with `env={**os.environ, "PYTHONIOENCODING": "utf-8"}`: piped stdout
on Windows otherwise re-encodes with the ANSI code page before the decode ever
sees it. Standalone scripts that cannot
import the package write `encoding="utf-8", errors="replace"` inline. A child
that genuinely writes in the console encoding (`ps`, `systeminfo`, user shells)
keeps locale decoding and says so with an inline `# subprocess-encoding: locale`
marker — an audit trail, not an escape hatch.

PowerShell is the one console-encoding child whose emit side we *can* pin, the
same move as `PYTHONIOENCODING` for a Python child: prefix the command with
`[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false);` and then
`**UTF8_TEXT` is correct rather than forbidden, because the encoding is now
known. Pinning only this end raises `UnicodeDecodeError` on a legacy code page,
and leaving both ends on the code page silently best-fits an unrepresentable
character away. Use the BOM-less `UTF8Encoding` spelling so no host prepends a
byte-order mark to the first field.

## Frontend

Icons are `lucide-react` components with `className="lucide-inline"`. Never an
emoji in the UI, never a hand-rolled SVG.

Never hardcode a user-facing English string, and never format a date, number or
sort order without naming a locale. Both are CI-gated; see
[../../ci/i18n-gates.md](../../ci/i18n-gates.md). Backend-owned strings (built-in
app manifests, HTTP error bodies) have no catalog path, so a new non-2xx JSON body
MUST carry a machine-readable `code` field the frontend can translate.

Full frontend conventions live in `website/AGENTS.md` and `website/docs/`.
