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

Set via the dashboard Welcome view (ghost button), Slack (`!incognito` or
`!temporary` prefix), or the mode icon in the chat header.

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

## Editing Memory

- **Dashboard**: Overview → Memory tab → edit preferences.md or projects.md
- **CLI**: `kirocrew memory show` / `kirocrew memory edit`
- **Chat**: ask Kiro Crew to update its memory files directly
