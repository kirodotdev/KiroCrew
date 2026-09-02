# Steering

Source: https://kiro.dev/docs/steering/ (the former `/docs/cli/steering/` now
redirects there — CLI and IDE steering are documented as one page).

Persistent project knowledge via markdown files. Instead of explaining conventions every chat, steering files ensure Kiro follows your patterns.

## Scope

| Location | Scope | Use case |
|----------|-------|----------|
| `.kiro/steering/` | Workspace | Project-specific patterns |
| `~/.kiro/steering/` | Global | Personal conventions across all projects |

Workspace steering takes priority over global on conflicts.

## Inclusion modes

Front matter declares when a document applies:

```yaml
---
inclusion: fileMatch
fileMatchPattern: "src/**/*.ts"
---
```

`always` (the default), `fileMatch` + `fileMatchPattern`, `manual` (referenced
from chat as `#<file-name>`), and `auto` + `name` + `description` (included when
the request matches the description).

**What kiro-cli actually does with them differs from the IDE.** Measured against
**2.19.1** over ACP, driven with `acp --agent <name>` + `session/set_mode` and the
`initialize` / `session/new` / `session/prompt` shapes `acp/client.py` sends,
against a workspace holding one document per mode with a unique marker in each:

| Declared | Loaded |
|---|---|
| `always` (workspace and `~/.kiro/steering` alike) | yes |
| `manual` | no |
| `manual`, referenced from the prompt as `#<name>` | no — there is no ACP-side way to invoke one |
| `fileMatch`, with a file under its pattern read in the same turn | no — withheld, and never matched |
| `auto`, with a request unrelated to its `description` | yes — loaded regardless |
| an unrecognized value (`inclusion: totallyBogusMode`) | yes |

**The table above is a snapshot of 2.19.1, and the harness has moved since.** On
**2.20.0** a maintainer reports that kiro-cli pulls a `fileMatch` document in by
itself part-way through a turn — recording the files a tool reads or writes and
appending the documents whose patterns match — and that an `auto` document is no
longer loaded into every session but offered on demand through the same catalog
mechanism skills use. Re-measuring here on 2.20.0 with the default engine (v2)
and `kiro_default` did NOT reproduce either change, in single-turn or two-turn
form, so the difference is a condition — engine or agent configuration — rather
than the version alone. Treat every row below as "measured under stated
conditions", never as a standing property of the harness.

The one row both measurements agree on is `manual`: kiro-cli has no way to bring
such a document into a turn.

So on the ACP path as measured at 2.19.1 `manual` is honored (kiro-cli 2.19.0 fixed that), `fileMatch`
is withheld and never applies, and `auto` behaves as `always` — consistent with
an unrecognized value falling through to the default rather than being matched.
`#[[file:...]]` references inside a loaded document are not expanded either.

Two corollaries for this repo:

- **The agent config's `resources` glob does not gate any of this.** An agent
  declaring `resources: []` receives exactly the same documents as one carrying
  `file://.kiro/steering/**/*.md`; kiro-cli scans both steering directories
  itself. See the comment on the seed in `agent.py`.
- **Below 2.19.0 the semantics differ** — `inclusion` was applied only on the
  `chat --no-interactive` path, so every document loaded regardless
  ([kirodotdev/Kiro#10794](https://github.com/kirodotdev/Kiro/issues/10794), and
  [#3026](https://github.com/kirodotdev/KiroCrew/issues/3026) where that was
  experienced here). Nothing pins a minimum kiro-cli version, so check the
  installed one before diagnosing a steering problem.

## Viewing and editing in Kiro Crew

The dashboard surfaces both locations under **Agent Capabilities → Steering**: it lists every `.md` file in `~/.kiro/steering` and the active project's `.kiro/steering`, renders the content, and supports creating, editing and deleting files. See `docs/system-specs/features/steering-viewer.md`.

## Foundational steering files

- `product.md` — product purpose, users, features, business objectives
- `tech.md` — frameworks, libraries, tools, constraints
- `structure.md` — file organization, naming, import patterns, architecture

Included in every interaction by default.

## Custom steering files

Create `.md` files in `.kiro/steering/` with descriptive names (e.g. `api-standards.md`).

## With custom agents

The docs state steering files are NOT auto-included in custom agents, and that
they must be added explicitly:

```json
{ "resources": ["file://.kiro/steering/**/*.md"] }
```

This does not hold on 2.19.1 — see the measurement above: a custom agent with an
empty `resources` list still receives both steering roots.

## AGENTS.md

Kiro supports `AGENTS.md` files (always included). Place in `~/.kiro/steering/` or workspace root.

## Best practices

- One domain per file
- Clear names: `api-rest-conventions.md`, `testing-unit-patterns.md`
- Include context (why, not just what)
- Provide code examples
- Never include secrets
- Review during sprint planning
