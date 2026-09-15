# Model Selection

Source: https://kiro.dev/docs/models/ and
https://kiro.dev/docs/models/available-models/ (fetched 2026-09-06)

Upstream retired `/docs/cli/chat/model-selection/`; the model catalogue is now
surface-agnostic and lives under `/docs/models/`. Model selection applies to the
chat experience only, interactive and non-interactive.

## Catalogue

Cost is a multiplier relative to Auto, so a task costing 10 credits on Auto costs
22 on an Opus tier and 0.5 on Qwen3 Coder Next. Two models on the same
multiplier can still bill differently, because generated tokens, thinking depth
and tokenizer differences all feed the count.

| Model | Context | Cost | Free tier |
|---|---|---|---|
| GPT-5.6 Sol | 272K | 2.4x | no |
| GPT-5.6 Terra | 272K | 1.0x | no |
| GPT-5.6 Luna | 272K | 0.1x | no |
| Claude Opus 5 | 1M | 2.2x | no |
| Claude Opus 4.8 | 1M | 2.2x | no |
| Claude Opus 4.7 | 1M | 2.2x | no |
| Claude Opus 4.6 | 1M | 2.2x | no |
| Claude Opus 4.5 | 200K | 2.2x | no |
| Claude Sonnet 5 | 1M | 1.3x | no |
| Claude Sonnet 4.6 | 1M | 1.3x | no |
| Claude Sonnet 4.5 | 200K | 1.3x | yes |
| Claude Sonnet 4.0 | 200K | 1.3x | yes |
| Auto | router | 1.0x | yes |
| Claude Haiku 4.5 | 200K | 0.4x | no |
| GLM-5 | 200K | 0.5x | yes |
| DeepSeek 3.2 | 128K | 0.25x | yes |
| MiniMax M2.5 | 200K | 0.25x | yes |
| MiniMax M2.1 | 200K | 0.15x | yes |
| Qwen3 Coder Next | 256K | 0.05x | yes |

The three GPT-5.6 tiers and DeepSeek 3.2, MiniMax M2.1 and Qwen3 Coder Next are
marked experimental; the rest are active. An experimental model may be served
from commercial AWS Regions worldwide rather than the geography matching the
profile, and GPT-5.6 is served from the US whatever the profile region.

## Auto

Kiro's router, with automatic fallback. Free tier gets Sonnet-class quality or
better and paid tiers Opus-class or better. Governance treats Auto as its own
selectable option: an administrator can allow or block it, but an enabled Auto
does not confine its routing to the administrator's approved set, though it never
routes to an experimental model. Guaranteeing only approved models means blocking
Auto and pinning an approved default.

## Switching models

```bash
kiro-cli settings chat.defaultModel claude-opus-4.8
```

```
> /model set-current-as-default
```

The preference is stored in `~/.kiro/settings/cli.json` and new sessions pick it
up. A model missing from the in-chat dropdown usually appears after a client
restart.

## Reasoning effort

`/effort`, or the `--effort` launch flag, sets the level; the picker offers only
the levels the current model supports and the choice persists. A higher level
spends more tokens internally, so it costs more credits at the same multiplier.
Kiro Crew's own resolution order and per-role behaviour are documented in
[providers](../../../system-specs/modules/providers.md).

## Choosing guide

- **Auto** — general development, cost efficiency, mixed task types
- **GPT-5.6 Sol** — hardest long-horizon refactors and terminal work
- **GPT-5.6 Terra** — routine multi-step development at a 1.0x multiplier
- **GPT-5.6 Luna** — high-frequency agentic work where throughput dominates
- **Opus 5** — highest reliability, multi-agent coordination, full-task completion
- **Sonnet 5** — near-Opus agentic behaviour at Sonnet cost
- **Haiku 4.5** — quick iterations, simple fixes, subagents
- **MiniMax M2.5 / GLM-5 / Qwen3 Coder Next** — frontier-competitive coding at
  the lowest multipliers, for long sessions on a budget
