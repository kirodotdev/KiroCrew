# Agent Skills

Source: https://kiro.dev/docs/skills/ (fetched 2026-09-06; the former
`/docs/cli/skills/` redirects there — skills are documented as one page across
every surface).

Portable instruction packages that extend what Kiro knows. Follows the open [Agent Skills](https://agentskills.io) standard.

## How skills work

Progressive disclosure in three steps: at session start Kiro reads only each
skill's name and description; when a request matches a description it loads the
full instructions; scripts and reference files load only when the instructions
direct it to.

```bash
> /context show    # see available skills
```

## Invoking a skill

A skill activates automatically on a description match, and every skill is also a
slash command, so a skill named `pr-review` is `/pr-review`. Text after the
command is passed to the agent as extra context. If the skill body contains
`$ARGUMENTS` or `$` placeholders, that text is substituted into them —
placeholder substitution is CLI-only.

```text
> /pr-review focus on the authentication changes
```

## Skill locations

| Location | Scope | Use case |
|----------|-------|----------|
| `.kiro/skills/` | Workspace | Project-specific workflows |
| `~/.kiro/skills/` | Global | Personal workflows across projects |

On a name collision workspace skills take priority. The default agent loads both
locations automatically.

### Custom agents

Must explicitly add skills, using the `skill://` scheme, which accepts specific
paths, glob patterns and home-directory expansion:

```json
{
  "resources": [
    "skill://.kiro/skills/*/SKILL.md",
    "skill://~/.kiro/skills/*/SKILL.md"
  ]
}
```

## Installing a skill on the CLI

Copy the skill folder into `.kiro/skills/` for the workspace or `~/.kiro/skills/`
for global scope. Discovery happens when a new chat session starts.

## Creating a skill

```
pr-review/
├── SKILL.md           # Required
├── scripts/           # Optional executable code
├── references/        # Optional documentation
└── assets/            # Optional templates
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
| `name` | Yes | Must match the folder name. Lowercase letters, numbers, hyphens. Max 64 chars. |
| `description` | Yes | When to activate. Max 1024 chars. |
| `license` | No | License name, or a reference to a bundled license file. |
| `compatibility` | No | Environment requirements, such as required tools or network access. |
| `metadata` | No | Extra key-value data such as author or version. |

### Reference files

Put extensive docs in `references/` folder. Kiro loads them only when instructions direct it to.
