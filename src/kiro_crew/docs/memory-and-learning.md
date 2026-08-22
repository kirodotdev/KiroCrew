# Memory & Learning

Kiro Crew has persistent memory that survives across sessions. It remembers your
preferences, project context, daily activity, and corrections you teach it.

## Memory Types

### Preferences (`preferences.md`)

Your personal preferences — coding style, tools you prefer, communication
style. Updated automatically by the consolidator after ~30 messages.

### Projects (`projects.md`)

Active project context — what you're working on, key decisions, blockers.
Updated alongside preferences.

### Daily History (`history/{date}.md`)

Conversation summaries organized by date. Natural decay:
- Last 14 days: full detail (days 0–13)
- 14–60 days: first entry per day + count
- 61–180 days: date + entry count only
- 181–365 days: retained on disk but not loaded into context
- 365+ days: pruned automatically

### Lessons (`lessons.jsonl` or vector store)

Corrections and rules you teach Kiro Crew. Two ways to create:
1. **Explicit**: say "remember to always use pytest" → saved immediately
2. **Implicit**: correct Kiro Crew during conversation → extracted during consolidation

Lessons have two scopes:
- **Global** (default): shared across all workspaces
- **Workspace**: only visible in the current workspace

## Memory Modes

Each session can operate in one of three memory modes:

| Mode | Reads Memory | Writes Memory | Consolidates | Use Case |
|------|-------------|---------------|-------------|----------|
| **Persistent** (default) | ✅ | ✅ | ✅ | Normal work |
| **Incognito** | ✅ | ❌ | ❌ | Sensitive tasks — reads context but blocks learn_add and consolidation |
| **Temporary** | ❌ | ❌ | ❌ | Isolated experiments — no memory interaction at all |

Set via the dashboard Welcome view (ghost button), the mode icon in the chat
header, Slack (`!incognito` / `!temporary` prefix), or Telegram (`/incognito` /
`/temporary`). Telegram spells them as commands because it has a command grammar;
the modes, the guarantees and the durability are the same on both channels, and
both accept a question after the modifier to mark the conversation and answer in
one message.

All modes still write session JSONL files (for history/resume). Incognito
blocks learn_add and consolidation. Temporary additionally blocks memory
reads — no preferences, history, or lessons are injected into the prompt.

## Teaching Kiro Crew

Just tell it naturally:
- "Always use dark mode"
- "Never use `rm -rf` without confirmation"
- "Remember that our team uses pytest-asyncio strict mode"
- "Prefer ruff over flake8 for linting"

Kiro Crew saves these via the `learn_add` MCP tool. View them with `learn_list`
or on the dashboard Overview → Lessons tab.

## Workspaces

Memory is workspace-scoped. Different workspaces have different preferences,
projects, and history. Lessons can be global or workspace-specific.

## Vector Memory

Semantic search over your memory, always on:

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
- **Chat**: ask Kiro Crew to update its memory files directly
