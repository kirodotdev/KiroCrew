# Agent Skills

Source: https://kiro.dev/docs/cli/skills/

Portable instruction packages that extend what Kiro knows. Follows the open [Agent Skills](https://agentskills.io) standard.

## How skills work

Kiro discovers skills by reading names/descriptions at session start. When your request matches, Kiro loads full instructions automatically. No slash command needed.

```bash
> /context show    # see available skills
```

## Skill locations

| Location | Scope | Use case |
|----------|-------|----------|
| `.kiro/skills/` | Workspace | Project-specific workflows |
| `~/.kiro/skills/` | Global | Personal workflows across projects |

Workspace skills take priority. Default agent loads both automatically.

### Custom agents

Must explicitly add skills:

```json
{
  "resources": [
    "skill://.kiro/skills/*/SKILL.md",
    "skill://~/.kiro/skills/*/SKILL.md"
  ]
}
```

## Creating a skill

```
pr-review/
├── SKILL.md           # Required
└── references/        # Optional
    └── checklist.md
```

### SKILL.md format

```markdown
---
name: pr-review
description: Review pull requests for code quality, security issues, and test coverage.
---

## Review checklist
1. Check for vulnerabilities, injection risks, exposed secrets
2. Verify edge cases and failure modes
3. Confirm new code has tests
4. Ensure clear naming
```

### Frontmatter fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Lowercase letters, numbers, hyphens. Max 64 chars. |
| `description` | Yes | When to activate. Max 1024 chars. |

### Reference files

Put extensive docs in `references/` folder. Kiro loads them only when instructions direct it to.
