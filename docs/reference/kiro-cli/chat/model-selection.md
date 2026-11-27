# Model Selection

Source: https://kiro.dev/docs/models/ and
https://kiro.dev/docs/models/available-models/ (fetched 2026-11-23)

Upstream retired `/docs/cli/chat/model-selection/`; the model catalogue is now
surface-agnostic and lives under `/docs/models/`. Model selection applies to the
chat experience only, interactive and non-interactive.

## Catalogue

Cost is a multiplier relative to Auto, so a task costing 10 credits on Auto costs
20 on Opus 5.5, 22 on Opus 5 and 0.5 on Qwen3 Coder Next. Two models on the same
multiplier can still bill differently, because generated tokens, thinking depth
and tokenizer differences all feed the count.

| Model | Context | Cost | Free tier |
|---|---|---|---|
| GPT-5.6 Sol | 1M | 4.4x* | no |
| GPT-5.6 Terra | 1M | 2.2x* | no |
| GPT-5.6 Luna | 1M | 0.6x* | no |
| Claude Fable 5.1 | 1M | 6x | no (Enterprise only) |
| Claude Opus 5.5 | 1M | 2.0x | no |
| Claude Sonnet 5.5 | 1M | 1.3x | no |
| Claude Haiku 5.5 | 1M | 0.06x* | no |
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
| Grok 4.7 | 500K | 2.7x* | no |
| Kimi K3 | 1M | 2.4x | no |
| DeepSeek 3.2 | 128K | 0.25x | yes |
| MiniMax M2.5 | 200K | 0.25x | yes |
| GLM-5 | 200K | 0.5x | yes |
| MiniMax M2.1 | 200K | 0.15x | yes |
| Qwen3 Coder Next | 256K | 0.05x | yes |

\* Two-tier context pricing. GPT-5.6 doubles its rate past 272K tokens (Sol 8.8x,
Terra 4.4x, Luna 1.2x), Haiku 5.5 goes to 0.3x past 100K tokens, and Grok 4.7 goes
to 5.4x past 200K tokens.

Fable 5.1 is for Enterprise organizations only: an administrator adds it to the
approved list, and its inference runs only in US East (N. Virginia). Grok 4.7,
DeepSeek 3.2, MiniMax M2.1 and Qwen3 Coder Next are marked experimental; Claude
Sonnet 4.0 reached end of life on 2026-10-14; the rest are active. Grok 4.7, Kimi
K3, DeepSeek 3.2 and GLM-5 are served from the US only. GPT-5.6 has Global
inference scope, so it may be served from commercial AWS Regions worldwide and
always uses the US endpoint, whatever the profile region.

## Auto

Kiro's router, with automatic fallback. Free tier gets Sonnet-class quality or
better and paid tiers Opus-class or better. Governance treats Auto as its own
selectable option: an administrator can allow or block it, but an enabled Auto
may route to any active model the profile can reach, including models outside the
administrator's approved list, though it never routes to an experimental model.
Guaranteeing only approved models means blocking Auto and pinning an approved
default.

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

## Fallback model

When a model is unavailable or refuses a request, Kiro can move the turn to a
fallback model, and the transcript names the model that answered. A fallback you
set yourself covers both cases; a vendor-recommended fallback covers refusals
only. A fallback for an unavailable model lasts one turn; after a refusal the
conversation stays on the fallback. Set it in the CLI with `/model fallback`.
API-key sessions cannot read or save fallback settings.

## Reasoning effort

Open `/model` and choose Effort, or pass the `--effort` launch flag; the picker
offers only the levels the current model supports. Effort chosen through `/model`
persists for that model. A direct `/effort <level>` changes only the current
session unless you then run `/effort set-current-as-default`, and in V3
`--effort` is also session-only. A higher level spends more tokens internally, so
it costs more credits at the same multiplier. Kiro Crew's own resolution order
and per-role behaviour are documented in
[providers](../../../system-specs/modules/providers.md).

## Choosing guide

- **Auto** — general development, cost efficiency, mixed task types
- **GPT-5.6 Sol** — hardest long-horizon refactors and terminal work
- **GPT-5.6 Terra** — balanced routine multi-step development
- **GPT-5.6 Luna** — high-frequency agentic work where throughput dominates
- **Opus 5.5** — demanding long tasks that need sustained multi-file work and
  self-verification
- **Sonnet 5.5** — fast scoped coding, bug fixes, review, agent and subagent work
- **Haiku 5.5** — high-volume work that needs quick responses
- **MiniMax M2.5 / GLM-5 / Qwen3 Coder Next** — frontier-competitive coding at
  the lowest multipliers, for long sessions on a budget
