# minimal fixture

Populated `$KIROCREW_HOME` used by tests that need a non-empty
starting state without the full richness of the `rich` fixture. One of
everything the gateway expects to find: two workspaces, preferences,
projects, semantic memory, history, a paused + active cron, a hook, and
a single pinned chat session. No personal data — all names, IDs,
timestamps, and paths are synthetic.

Running `kirocrew gateway --seed minimal` against a fresh
`$KIROCREW_HOME` lands you on a clickable dashboard: memory tab shows
the workspace content, cron tab shows the two jobs, sessions tab shows
the pinned **Welcome to KiroCrew** starter. Useful as the default
fixture for pytest tests that need "populated enough to exercise the
dashboard" without having to author more content than necessary.

If you want a fully-populated dashboard (multiple sessions, a folder,
History panel content, an archived session), use `rich` instead —
`rich` layers extra sessions and folder metadata on top of the same
minimal skeleton.

## Layout

```
minimal/
├── fixture.yaml         # schema-version marker
├── README.md            # this file
├── config.json          # gateway config (sanitised)
├── crons.json           # 1 active + 1 paused cron
├── hooks.json           # 1 hook
├── sessions/
│   └── dashboard_starter.jsonl   # pinned welcome chat (metadata + 4 messages)
├── workspace/           # default workspace
│   └── memory/
│       ├── preferences.md
│       ├── projects.md
│       ├── semantic.md
│       └── history/
│           └── 2026-01-15.md
└── workspace-extra/     # second workspace (multi-workspace coverage)
    └── memory/
        ├── preferences.md
        └── semantic.md
```

## What's intentionally NOT included

`memory.db` and `memory_index.db` live at the root of a real
`$KIROCREW_HOME` but are intentionally **not** included here. Text and
JSONL files give deterministic byte-for-byte comparison in fixture
tests; SQLite binaries would require a build step to make them diffable
and reproducible.

Gateway bootstraps both DBs empty on first start — semantic vector
memory and the SEL event log populate as the agent runs. Tests that
need DB-backed content should either write to `memory.db` themselves
after seeding, or wait for the SQLite builder to land in a follow-up
CR.
