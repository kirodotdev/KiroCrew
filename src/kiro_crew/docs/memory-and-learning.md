# Memory & Learning

Kiro Crew has persistent memory that survives across sessions. It remembers your
preferences, project context, daily activity, and corrections you teach it.

## Private member context

A member with Memory V2 receives its identity, permanent rules, current briefing,
manual preference/project anchors, and admitted project guides when a conversation
starts. Unchanged follow-up turns do not resend that complete snapshot. Changes
refresh it once; a replacement also retires guides removed from the current source
list. Resume, compaction and a new provider conversation restore it.

Rules, ownership and required source readability are checked on every turn, even
when no snapshot is resent. Missing declared files or oversized required guides
stop the turn rather than silently dropping instructions. Failed, cancelled or
empty attempts do not count as delivery. Native-loaded persona/resources are not
copied into the initial prompt again when their exact startup content is known.
Manual, auto and file-matched steering are not promoted to always-on guidance.
This does not erase text already retained in a conversation or change Global V1.

## Member database

Memory V2 keeps each member's learned facts, rules, experiences, daily summaries,
source history, revisions, text index and vectors in one SQLite database. A member's
stable identity selects that database; renaming a member or changing its provider
or project does not move learned memory. Manual persona, rules, briefing and project
guides remain documents. Preference/project anchors are owner-managed for V2.

V2 recall does not update access timestamps or repair storage. Missing or damaged
memory is reported without replacing it; manual member essentials remain usable.
Backups include the SQLite database and manual preference/project anchors. Restore
validates the same member identity and waits for live handles to close.

The file layouts and age tiers below describe Global V1 and legacy named V1.
V2 retains full daily summaries and revisions in its database without age pruning
or learned Markdown/JSONL copies. Its summaries, accepted facts, rules and source
receipt publish atomically; retries do not duplicate an acknowledged source span.

## Memory Types

### Preferences (`preferences.md`)

Your personal preferences — coding style, tools you prefer, communication
style. Updated automatically by the consolidator after ~30 messages.

### Projects (`projects.md`)

Active project context — what you're working on, key decisions, blockers.
Updated alongside preferences.

### Daily History (`history/{date}.md`)

Conversation summaries organized by date. What a READ returns decays with age:
- Last 14 days: full detail (days 0–13)
- 14–60 days: first entry per day + count
- 61–180 days: date + entry count only
- 181–365 days: retained on disk, not returned by a read
- 365+ days: pruned automatically

The tiers above govern a dated READ (`read_recent_history`), not search: the
full-text index holds each history file's complete content, so `memory_recall`
can still surface a line from a day the dated read would have collapsed to a
count.

A new session carries none of these bodies either way. It carries a bounded index
of the last three days' headings, and the body arrives only when `memory_recall`
asks for it.

### Lessons

Corrections and rules you teach Kiro Crew. Two ways to create:
1. **Explicit**: say "remember to always use pytest" → saved immediately
2. **Implicit**: correct Kiro Crew during conversation → extracted during consolidation

Member lessons live in the member's SQLite database. Global V1 also supports the
legacy `lessons.jsonl` fallback when vector memory is unavailable.

Lessons have two scopes:
- **Global** (default): shared across all workspaces
- **Repository-scoped**: an optional `repo_scope` path fragment restricts a lesson to sessions whose active project is inside that repository tree

## Memory Modes

Each session can operate in one of three memory modes:

| Mode | Reads Memory | Writes Memory | Consolidates | Use Case |
|------|-------------|---------------|-------------|----------|
| **Persistent** (factory default) | ✅ | ✅ | ✅ | Normal work |
| **Incognito** | ✅ | ❌ | ❌ | Sensitive tasks — reads context but blocks learn_add and consolidation |
| **Temporary** | ❌ | ❌ | ❌ | Isolated experiments — no memory interaction at all |

For new dashboard chats, choose the default under **Settings → Chat → Sessions →
Default Memory Mode**. The choice is stored as
`dashboard.default_memory_mode`. An explicit Incognito or Temporary choice still
wins for that chat. App-owned chats, messaging channels, cron jobs, and direct API
callers keep their own mode selection and do not inherit this dashboard preference.

Set via the dashboard Welcome view (ghost button), the mode icon in the chat
header, Slack (`!incognito` / `!temporary` prefix), or Telegram (`/incognito` /
`/temporary`). Telegram spells them as commands because it has a command grammar;
the modes, the guarantees and the durability are the same on both channels, and
both accept a question after the modifier to mark the conversation and answer in
one message.

Persistent sessions retain history for resume. Incognito and Temporary keep new
conversation bodies in memory and do not write transcript, workflow or task
snapshots containing them. Incognito blocks learned-memory writes, including
lesson deletion and consolidation. Temporary additionally blocks memory reads,
including learned lessons and memory preference/history injection. Manual member
persona, rules and project context remain available without opening memory.

## Teaching Kiro Crew

Just tell it naturally:
- "Always use dark mode"
- "Never use `rm -rf` without confirmation"
- "Remember that our team uses pytest-asyncio strict mode"
- "Prefer ruff over flake8 for linting"

Kiro Crew saves these via the `learn_add` MCP tool. View them with `learn_list`
or on the dashboard Overview → Lessons tab.

## Workspaces

Markdown memory is workspace-scoped: each workspace stores `memory/preferences.md`, `memory/projects.md`, and `memory/history/{date}.md` beneath its workspace directory. Legacy JSONL lessons are also workspace-local; vector memory defaults to `memory.db` under the Kiro Crew data directory. Lessons are global unless their optional `repo_scope` restricts them to a project tree.

## Vector Memory

The vector-memory subsystem is always enabled:

- **Semantic memory**: structured key-value store with confidence scoring
- **Episodic memory**: conversation fragments searchable by meaning
- **Embeddings**: Qwen3-Embedding-0.6B running in-process (no Ollama or any
  other server to install — the runtime is bundled; no data leaves your machine)

The embedding model (~610MB) downloads automatically in the background the
first time the gateway starts, over HTTPS from the Kiro Crew CDN — failed
downloads retry automatically with backoff, and again on the next gateway
start; the Memory tab shows download progress. Once downloaded, the model
loads in the background too, so nothing ever waits on it. While the model is
downloading or loading, memory falls back to keyword search and switches to
semantic search as soon as the model is ready — no restart needed. Requires
~610MB disk for the model and ~700MB RAM once the model is loaded.

The bundled model is `qwen3-embedding:0.6b` (1024 dimensions). `KIROCREW_EMBED_MODEL_URL` overrides `memory.embed_model_url` for the download URL; `KIROCREW_EMBED_MODEL_PATH` or `memory.embed_model_path` selects a local GGUF instead of the bundled model.

## Rebuilding vectors after a model change

In Memory settings, use the warning's link to Embedding Model, then apply the
model again. Applying the same file is supported. This rebuilds vectors even
when an earlier apply missed a closed member store. Saved memories are retained;
keyword search remains available while vectors are rebuilt.

The request survives a gateway restart. Open stores are repaired first. Closed
or unavailable stores are reported as deferred and handled by explicit maintenance. A loaded
model does not mean every store has finished rebuilding. An unknown repair scope
means some stores could not be checked, not that they are empty or repaired.

Known setup warnings and errors follow the dashboard language. Paths and system
error details remain exact. Older servers and unknown status codes retain their
original diagnostic text.

## Consolidation

Kiro Crew automatically consolidates conversations into memory:
- **Preferences/projects**: every 30 messages per session
- **Daily history + lessons**: after 3 hours idle per session

No manual action needed — it happens in the background.

## Reading Memory Programmatically

The markdown layer is readable through the CLI, so consumers depend on an
interface rather than the on-disk layout:

- `kirocrew memory show [preferences|projects|history]` — print the markdown
  layer (all three when no target is given). `--format json` returns structured
  entries with `path`, `updated_at`, and `content`; `--since YYYY-MM-DD` limits
  history to days on or after that date.
- `kirocrew memory export --include-markdown` — add a `markdown` collection to
  the JSON export. Without the flag the export shape is unchanged.

Both run non-interactively (no TTY or editor needed), so they work from
scheduled jobs.

## Editing Memory

- **Dashboard**: Overview → Memory tab → edit preferences.md or projects.md
- **Chat**: ask Kiro Crew to remember or correct a fact through its memory tools
