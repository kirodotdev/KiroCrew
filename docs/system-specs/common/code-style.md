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
| ACP client timeouts | `acp/client.py` | `_INIT_TIMEOUT`, `_DEFAULT_PROMPT_TIMEOUT`, `_READ_TIMEOUT`, `_STALE_TURN_TIMEOUT`, `_TOOL_STALL_TIMEOUT`, `_WAIT_RESPONSE_MAX_TIMEOUT`, `_SEL_AUDIT_TIMEOUT_SECONDS`. |
| MCP protocol version | `mcp_shared.py` | The `initialize` reply's `protocolVersion` for the managed stdio servers. `mcp_cron.py` / `mcp_core.py` do not carry their own copy. |
| Credential keys | `config/loader.py` | `CRED_*` env-var names plus the `_CREDENTIAL_KEYS` tuple. |
| Dashboard port | `config/loader.py` | `_DEFAULT_PORT` (5476) and `DASHBOARD_PORT` (honors `KIROCREW_PORT`). `dashboard/origin.py` imports `_DEFAULT_PORT` rather than restating it. |
| Hook results and event names | `hooks.py` | `HOOK_PASSTHROUGH` / `HOOK_REPLY` / `HOOK_MODIFY` / `HOOK_INJECT_CONTEXT`, the tool verdicts `TOOL_ALLOW` / `TOOL_AUTO_APPROVE` / `TOOL_DENY`, and `HOOK_EVENT_*` / `HOOK_EVENTS`. |
| Memory paths and dir names | `memory.py` | `WORKSPACE_DIR_NAME`, `MEMORY_DIR_NAME`, `HISTORY_DIR_NAME`, `PREFERENCES_FILE`, `PROJECTS_FILE`. |
| Lesson limits | `learn.py` | `_LESSONS_FILE`, `_MAX_LESSONS_IN_CONTEXT`, `_MAX_LESSONS_TOTAL` (prune oldest past the total). |
| Cron limits | `cron.py` | `_CRONS_FILE`, `_STORE_VERSION`, `_MIN_INTERVAL_SECS`, `_JOB_TIMEOUT_SECS`, `_TIMER_POLL_SECS`, `_AUTO_PAUSE_THRESHOLD`, the reaper intervals, the skip-date horizon, the store file-lock timeouts, and the hourly/daily jitter caps. |
| Session and transcript limits | `history.py` | `SESSIONS_DIR_NAME`, `ARCHIVE_RETENTION_DAYS`, the JSONL rotation pair `_SESSION_MAX_BYTES` (2 MB) / `_SESSION_KEEP_LINES`, the file-lock timeouts, and the search caps (`SEARCH_MIN_CHARS`, `_SEARCH_SCAN_WINDOW`, `_TITLE_BOOST`). |
| Context budgets | `context.py` | `_CONTEXT_BUDGET_BASE` plus one `_budget(fraction)` cap per block (history, preferences, projects, lessons, semantic, episodic, skills, steering, compressed history, preamble headroom). Budgets are expressed as FRACTIONS of the base, so read them there rather than quoting a byte figure. |
| Task states | `task.py` | The `TaskState` enum, the `_TERMINAL` set, and the `_TRANSITIONS` map that defines the legal state machine. |
| Heartbeat intervals | `heartbeat.py` | `_DEFAULT_INTERVAL`, `_FTS_REBUILD_TICKS`, `_PRUNE_TICKS`, `HEARTBEAT_TASK_TIMEOUT_SECS`, `HEARTBEAT_FILE`. |
| Subagent limits | `subagent.py` | `_MAX_CONCURRENT`, `_TIMEOUT_SECS`, `_TURN_LIMIT`, `_MAX_DONE_RESULT_LEN`, `_STARTUP_TIMEOUT_SECS`, `INJECTION_TIMEOUT`, the reaper/stall intervals. |
| Slot state caps | `dashboard/state.py` | `_MAX_SLOT_MESSAGES`, `_MAX_PERSISTED_NOTIFICATIONS`, `_MAX_SOURCE_LINKS_PER_SLOT`, `_MAX_PENDING_CONTEXT`, `NATIVE_SUBAGENT_DONE_RESULT_CAP`, `_QUESTION_TIMEOUT_MAX`. |
| Injected-message envelope prefixes | `dashboard/state.py` | `CRON_NOTIFY_PREFIX` / `CRON_NOTIFY_END` / `CRON_NOTIFY_RE`, `SUBAGENT_COMPLETION_PREFIX`, `SUBAGENT_SYNTHESIS_PREFIX`, and the five `*_RECOVERY_PREFIX` markers. All in one place so the frontend has one list to mirror. See [injected-messages](injected-messages.md). |
| Usage cache TTLs | `dashboard/handlers/usage.py` | `_CACHE_TTL`, `_TOKEN_CACHE_TTL`, `_CONTEXT_CACHE_TTL`, `_TOKEN_HISTORY_DAYS`, `_CONTEXT_TOP_SESSIONS`. |
| Webhook hook limits | `dashboard/handlers/hooks.py` | `_HOOK_MAX_CONCURRENT` (semaphore-backed, 429 past it), `_HOOK_MESSAGE_MAX_LEN`, `_HOOK_TIMEOUT_DEFAULT` / `_HOOK_TIMEOUT_MAX` (both prime, to avoid a thundering herd with cron intervals). |
| Embed cache | `embeddings.py` | `_EMBED_CACHE_MAX` (128 entries, keyed by text plus model id; the comment there carries the memory arithmetic). |
| Slack UX strings and pacing | `slack/handler.py` | `_THINKING`, `_CURSOR`, `_NO_RESPONSE`, `_STATUS_WORKING`, `_TRUNCATION_MARKER`, plus `_EDIT_INTERVAL`, `_APPROVAL_TIMEOUT`, `_SLACK_SECTION_TEXT_LIMIT`, the stall thresholds and the phase debounce. |
| Cross-cutting shared constants | `constants.py` | `KIROCREW_SPAWNED_ENV`, `ENV_TRUTHY`, `CHAT_TURN_TIMEOUT`, the `[OPTIONS:]` parse regexes, `DATA_WARNING`, `BANNER`. |
| Gateway shutdown budget | `gateway_shutdown_budget.py` | Gateway cooperative timeout, service-manager signal margin, and the derived systemd/launchd stop deadline. |
| Process-wide shutdown signal | `__init__.py` | `shutdown_event`. Background loops `await shutdown_event.wait()` with a timeout instead of a plain `asyncio.sleep`, so they wake instantly on Ctrl-C. |
| Base agent config | `config/defaults.json` | `tools`, `allowedTools`, `resources`, `hooks`, model. Packaged as package data, so editing it needs no code change. |
| Managed MCP server specs | `agent.py` | `_MANAGED_MCP_SERVERS`: which servers are auto-registered and refreshed while preserving user customizations. |
| Built-in skills | `builtin_skills/<name>/SKILL.md` | Frontmatter (`always`, `triggers`, `dir`) is the skill's own contract. This is the only tree copied into a user's `~/.kiro/crew/skills/`. |

Other style rules:

| Rule | Requirement |
|---|---|
| Line length | 100 chars (black and isort are both configured to it) |
| Python version | >= 3.10; `from __future__ import annotations` for type hints |
| Imports | `import logging` plus `logger = logging.getLogger(__name__)` |
| Async | `asyncio` throughout; `async def` for all I/O |
| Dataclasses | `@dataclass` for data containers |
| Errors | Custom exceptions in `acp/client.py`; return error strings at tool boundaries. See [error-handling](error-handling.md). |

## Comments explain the WHY

A comment carries what the code cannot: invariants, edge cases, units,
non-obvious constraints, and the reason a surprising choice is correct. It is not
a restatement of the line below it, and it is not a task log.

Do NOT put in a comment or docstring:

- PR or review numbers, review-round or finding markers, ticket ids
- incident dates, milestone tags, commit SHAs
- historical narration: "previously", "used to", "we now", "Status: implemented"

That history lives in git. State CURRENT behavior in present tense. A comment that
narrates a change is stale the moment the next change lands, and a reader cannot
tell whether it describes the code in front of them.

Keep them concise. `_vendor/` (vendored third-party code) and pragma comments
(`# type: ignore`, `# noqa`) are exempt.

## The lint pitfalls

The blocking gates are isort, flake8 and mypy (`black --check` is not yet
enabled). Run them before committing:

```bash
black src/kiro_crew test && isort src/kiro_crew test
flake8 src/kiro_crew test && mypy src/kiro_crew
python -m pytest
```

| Gate | Rule | Detail |
|---|---|---|
| flake8 F401 | No unused imports. | Remove any import not directly used in the file. A pyflakes check, always on. |
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

## Frontend

Icons are `lucide-react` components with `className="lucide-inline"`. Never an
emoji in the UI, never a hand-rolled SVG.

Never hardcode a user-facing English string, and never format a date, number or
sort order without naming a locale. Both are CI-gated; see
[../../ci/i18n-gates.md](../../ci/i18n-gates.md). Backend-owned strings (built-in
app manifests, HTTP error bodies) have no catalog path, so a new non-2xx JSON body
MUST carry a machine-readable `code` field the frontend can translate.

Full frontend conventions live in `website/AGENTS.md` and `website/docs/`.
