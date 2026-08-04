# Steering

Source: https://kiro.dev/docs/cli/steering/

Persistent project knowledge via markdown files. Instead of explaining conventions every chat, steering files ensure Kiro follows your patterns.

## Scope

| Location | Scope | Use case |
|----------|-------|----------|
| `.kiro/steering/` | Workspace | Project-specific patterns |
| `~/.kiro/steering/` | Global | Personal conventions across all projects |

Workspace steering takes priority over global on conflicts.

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

Steering files are NOT auto-included in custom agents. Add explicitly:

```json
{ "resources": ["file://.kiro/steering/**/*.md"] }
```

## AGENTS.md

Kiro supports `AGENTS.md` files (always included). Place in `~/.kiro/steering/` or workspace root.

## Best practices

- One domain per file
- Clear names: `api-rest-conventions.md`, `testing-unit-patterns.md`
- Include context (why, not just what)
- Provide code examples
- Never include secrets
- Review during sprint planning
