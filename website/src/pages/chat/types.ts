import type { ChatMessage } from '../../types'

export const SOFT_STOP_DEBOUNCE_MS = 150

/**
 * Literal present in every `spawn_run` launch result, in both output shapes
 * (bare text and the MCP result envelope).
 *
 * Shared because two layers need the same gate: `chatSlice.sseToolResult` uses
 * it to decide whether a tool result is worth copying onto the tool message's
 * meta at all, and `SubagentRunCard` uses it as a cheap reject before parsing.
 * Tool output can reach the server's 1 MB cap and `state.messages` is not
 * capped the way `toolLog` is (100 entries), so a blanket copy would let a long
 * turn grow the heap without bound.
 */
export const SPAWN_LAUNCH_MARKER = 'subagent(s).'

export type TurnItem =
  | { kind: 'single'; msg: ChatMessage; idx: number }
  | { kind: 'group'; msgs: ChatMessage[]; startIdx: number }

export type DisplayItem =
  | TurnItem
  | {
      kind: 'turn'
      items: TurnItem[]
      complete: boolean
      /**
       * This turn is the INTERIM work of a sub-agent fan-out: everything the
       * agent emitted between the user's prompt and the synthesis turn that
       * restates it. Set by `groupDisplayItems` when it sees the synthesis
       * injection, and read by TurnBlock, which folds the whole region behind
       * one toggle so the reader lands on the synthesis instead of on N
       * per-completion summaries that say the same thing.
       */
      interim?: boolean
    }
