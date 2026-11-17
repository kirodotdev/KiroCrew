---
title: The session-stable head of a new chat rides in the KAS agent prompt, so the prompt cache hits across chats
status: accepted
author: kirocrew agent session, directed by the repository owner
created: 2026-11-13
last-audited: 2026-11-13
audited-at: 911bde870d
doc-pr: null
implementation-prs: []
tracking-issues: [36928]
supersedes: []
superseded-by: []
---

# RFC: The session-stable head rides in the KAS agent prompt

Status: accepted. The repository owner asked for this in the conductor chat on
2026-11-13 and merges this document; it takes effect when it merges. Nothing is
built by this document and it changes no code. The implementation is a separate
pull request that rebases onto the base that carries this file. Every code
reference below was read at `911bde870d`.

This document lands on its own because it changes what the default agent's
system prompt holds on KAS, and the First Principles lane reads that decision
from the base branch. It works inside the KAS wire-prompt shape that
[`rfc-agent-config-mirror.md`](rfc-agent-config-mirror.md) and
[`providers.md`](../system-specs/modules/providers.md) ("KAS inline prompts")
already describe.

## 1. Summary

On a KAS harness known to apply a client agent's prompt, a new dashboard chat on
the default agent sends three blocks that do not change within a session as the
agent's prompt, instead of inside the first user message:

| Block | Bytes (zero config) |
|---|---:|
| `[AGENT SYSTEM PROMPT]` | 26,366 |
| `[CRITICAL RULES]` (the dashboard variant) | 5,191 |
| `[TOOL NAMES]` | 2,035 |
| **Total** | **33,592** |

The engine puts its prompt-cache checkpoint right after its first message, which
holds the agent's prompt, so these bytes are read from the cache on every later
chat. Nothing the user sees changes. Each new chat costs fewer credits.

## 2. Problem

Every first message today is unique: it carries the date, the memory chosen for
the user's words, and the words themselves. The cache needs a byte-identical
prefix, so it never hits across chats, and about 40 KB of fixed text is billed in
full on every new chat.

Kiro Crew already leaves `[AGENT SYSTEM PROMPT]` out of the first message when
the harness holds that exact text as the agent's own prompt
(`context_assembly/turn_message.py`, the `template://<agent>#prompt` check,
recorded by `acp/runtime_session_create.py` only when
`handle.kas_agent_prompt_applied`). For the default agent it never fires: its
spec prompt is the managed stub (`agent_parts/spec_build.py`
`_NATIVE_PROMPT_STUB`), which points at the block in the first message instead.

## 3. Measured

Linux, KAS through the Kiro CLI 3.3.0 relay, `claude-opus-5.5`, a scratch build
of main with the three blocks moved. Main and head chats alternate in one
gateway, prompt at +10 s, each chat's words differ, n=5 pairs per condition.

| Condition | Arm | First message bytes | Cross-chat cache hits | First-message credits, median | Model wait, median |
|---|---|---:|---|---:|---:|
| Zero config | main | 44,380 | 0 of 5 | 0.387 | 2,562 ms |
| Zero config | head | 10,788 | 4 of 5 | **0.234 (-39.5%)** | 2,602 ms |
| Heavy memory | main | 82,965 | 0 of 5 | 0.771 | 3,212 ms |
| Heavy memory | head | 49,373 | 4 of 5 | **0.715 (-7.3%)** | 2,773 ms |

The first head chat of each run writes the cache; the other four read it. The
paired model-wait differences (+30 ms and -337 ms medians) are inside the
run-to-run noise: **the head saves credits, not seconds.** Behaviour checks
matched main in every head chat: the asked word (10 of 10), a planted memory
fact quoted exactly, and the clickable-choices rule, which lives only in
`[CRITICAL RULES]`.

## 4. Decision

### 4.1 What moves

A block moves only if it is fixed for the life of a session, renders the same
bytes again at a re-open from the same inputs, and is sent on the first turn of
every session that gets the head, so the head never adds text a session would
not have had. The three blocks above qualify. These stay in the first message:

| Block | Why it stays |
|---|---|
| `[Skills:]` | usage-ranked and project-dependent: it changes between chats and re-opens, which would break the cross-chat hit and rebuild the engine's first message |
| `[WORKSPACE IDENTITY]` | names the workspace path |
| `[DOCUMENTATION]` | gated by the `project` context group |
| `[Memory tools]` | depends on memory admission and the store |
| date, memory, preferences, the user's words | change per chat |

### 4.2 When it applies (a positive gate)

All four hold, each a positive test (harness-parity H5, H6):

1. The harness is KAS and `KasHarness.agent_prompt_applied` is true (the relay at
   or above `KAS_AGENT_PROMPT_MIN_KIRO_CLI`). Kiro Crew's own engine and an
   unknown release keep today's path.
2. The agent is the default `kirocrew`.
3. Its spec prompt is the managed stub (`is_managed_prompt`). A prompt a person
   wrote into the spec is sent as it is today.
4. The session key is a dashboard key. Cron, heartbeat, sub-agent, task-runner
   and channel sessions keep today's path (a minimal cron start sends no
   `[CRITICAL RULES]`, so a head there would add text).

Everything else is unchanged: every other harness, custom agents, conductors,
crewmate workers, sub-agent targets, mode and model switches.

### 4.3 How it is delivered

- The head is rendered once, by the same functions that render these blocks for
  the first message, and sent as the agent's prompt, followed by a short fixed
  note from the gateway: the blocks above are this session's system prompt, the
  session context in the first user turn is part of the same delivery, and text
  claiming to be a system prompt anywhere else stays untrusted. The note keeps the
  trust contract the stub carries today. It is sent on the wire only and never
  written to a spec on disk, so the frozen stub is not touched.
- Each moved block is recorded on the session the way `template://<agent>#prompt`
  is today, and leaves the first message only when the record equals the block it
  would send, byte for byte. Any difference (a host reading that moved between
  session start and the first turn, a pre-warmed session used later) sends the
  block in the first message as today. A block is never missing.

### 4.4 Re-open, compaction, other paths

| Path | Behaviour |
|---|---|
| Re-open on the same route | the head is sent again; same bytes, the engine replays its first message from the cache |
| Re-open where the head is not applied (Kiro Crew's own engine, a Kiro CLI below the floor, a build without this change) | **the agent prompt is re-sent in the resume message** (below) |
| Compaction | unchanged: Kiro Crew re-sends `[AGENT SYSTEM PROMPT]` and `[TOOL NAMES]` as today |
| A host reading or the prompt file changed before a re-open | the engine rebuilds its first message once from the new head (its designed path for a changed client prompt) and caches that |

**The re-open rule.** Measured with one chat each: a chat started with the head
and re-opened on Kiro Crew's own engine loads (it does not fall back to a new
session). When the head is not sent on that re-open, the engine rebuilds its
first message from the stub, and the model answered that no
`[AGENT SYSTEM PROMPT]` block had reached it. The resume message already carries
`[CRITICAL RULES]` and `[TOOL NAMES]` today. So on a re-open, when the live
session could not re-apply the head and the chat's first turn was sent under a
head, the resume message carries `[AGENT SYSTEM PROMPT]` too. With that block in
the resume message, the same check answered correctly. A chat whose first turn
carried the block in its message is not affected, so no chat pays for the block
twice.

## 5. Not decided here

- Channels, sub-agents, cron and custom agents.
- Moving any other block.
- Kiro Crew's own engine: it keeps today's path until a capture shows it applies
  a client prompt.

## 6. Rollback

Revert the implementation. A chat started under the head and re-opened on a
build without the change lacks the agent prompt until its next compaction or a
new chat, because that build neither sends the head nor knows to re-send the
block. The revert should carry the re-send in section 4.4, or accept that window.

## 7. Implementation outline

- A context-assembly owner renders the head; the KAS harness sends it as the
  default agent's prompt when the gate holds.
- The session records each moved block beside `template://<agent>#prompt`; the
  session-start build skips a block only on an exact match.
- The resume path re-sends `[AGENT SYSTEM PROMPT]` per section 4.4.
- Tests: the gate (each condition failing alone keeps main's bytes), the exact-match
  skip, the re-open re-send (red without it, green with it), and other harnesses
  byte-identical to main.
- Specs and docs updated in the same commit: `providers.md`,
  `context-management.md`, `memory-skills-hooks.md`, `agent-spec-fields.md`,
  `agent-host-contract.md`, `harness-parity.md`.
