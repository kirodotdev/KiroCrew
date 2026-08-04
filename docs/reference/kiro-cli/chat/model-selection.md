# Model Selection

Source: https://kiro.dev/docs/cli/chat/model-selection/

## Available models

### Auto (recommended, default)

Intelligent model router. ~23% less expensive than direct Sonnet 4. Uses best-in-class LLMs (Claude Sonnet 4 and alike) with smart routing per task.

### Claude Opus 4.6 (experimental)

State-of-the-art coding and agentic capabilities. Best for long-horizon tasks, large codebases, complex debugging, deep reasoning. Pro/Pro+/Power tiers.

### Claude Opus 4.5

Maximum intelligence for complex specialized tasks, professional SE, advanced agents. Pro/Pro+/Power tiers.

### Claude Sonnet 4.5

Best for complex agents and coding. Advanced SWE-bench scores, extended autonomous operation, enhanced reasoning.

### Claude Sonnet 4.0

Direct access, predictable behavior, no routing layers.

### Claude Haiku 4.5

Fastest model, near-frontier intelligence matching Sonnet 4. 2x+ speed, 1/3 cost. First Haiku with extended thinking.

## Cost comparison

| Model | Credit Usage | Example Cost |
|-------|-------------|-------------|
| Opus 4.6 | 2.2x | 22 credits |
| Opus 4.5 | 2.2x | 22 credits |
| Sonnet 4.5 | 1.3x | 13 credits |
| Sonnet 4.0 | 1.3x | 13 credits |
| Auto | 1.0x | 10 credits |
| Haiku 4.5 | 0.4x | 4 credits |

## Switching models

```bash
# CLI setting
kiro-cli settings chat.defaultModel claude-opus4.6

# In chat — persist current model as default
> /model set-current-as-default
```

Preference saved to `~/.kiro/settings/cli.json`.

## Choosing guide

- **Auto** — general dev work, cost efficiency, variable task types
- **Haiku 4.5** — speed-critical, high-volume, real-time apps
- **Sonnet 4.0/4.5** — consistency, model transparency, agent workflows
- **Opus 4.5** — maximum intelligence, professional SE, critical implementations
- **Opus 4.6** — long-running agentic tasks, large codebases, complex debugging
