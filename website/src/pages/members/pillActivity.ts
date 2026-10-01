/**
 * The second line of the crewmate DM header's identity pill: what the crewmate
 * is doing RIGHT NOW, in one short line under its name.
 *
 * The line is always present (the pill never changes height), and it is text
 * only — no dot, no glyph. Presence already has a home (the avatar's own
 * working state), so a second marker here would say the same thing twice.
 *
 * The busy readings come from the ONE seam the dashboard already resolves a
 * slot's live status through: `slotStatusDetail[slot]` (written by the
 * websocket layer on every turn start, reasoning burst, chunk and tool call)
 * rendered by `toolStatusLabel`, exactly as the sessions sidebar and the
 * command palette render theirs. So the pill honours the user's
 * `simplifiedToolNames` preference (purpose in simplified mode, the verbatim
 * command in raw mode, the argument-derived title where the raw one is a
 * stub) and names a moment the same way the sidebar row does. This module
 * adds only what is pill-specific:
 *
 *   reading                   | what the line says
 *   --------------------------|-----------------------------------------------
 *   `tool` status, still open | the shared label (purpose / derived / raw)
 *   `tool` status, returned   | the shared "thinking" copy — the model is
 *                             | reading the result, so the old purpose is stale
 *   `thinking`                | the shared copy for that phase (or a server-
 *                             | supplied status label, verbatim)
 *   `streaming`               | "Writing…" — the pill's own word. The seam's
 *                             | copy is "Streaming", transport jargon a reader
 *                             | of this line has no reason to know; a line that
 *                             | says what the crewmate is DOING says writing
 *   run state `compacting`    | "Compacting…"
 *   run state `stopping`      | "Stopping…"
 *   busy, no status yet       | "Working" (the slots frame says busy before the
 *                             | first status frame), or the delegated variant
 *                             | when only sub-agents are running
 *   not busy                  | "Idle · <time ago>", or just "Idle"
 *
 * Every shared label is clamped to `PILL_ACTIVITY_MAX_CHARS` code points with
 * an ellipsis: the pill sits centred over the transcript and a long purpose
 * sentence would push it to the header's full width. The fixed labels never
 * reach the cap.
 */
import type { SlotState, SlotStatusDetail } from '../../store/chatSlice'
import type { ToolStatusDetail } from '../../utils/toolStatusLabel'

/** Longest activity line, in code points, before it is cut with an ellipsis. */
export const PILL_ACTIVITY_MAX_CHARS = 40

/** The kinds the line can name. `tool` and `thinking` carry text from the
 *  shared status seam; the rest are catalog strings the page owns. */
export type PillActivityKind =
  | 'tool'
  | 'thinking'
  | 'writing'
  | 'compacting'
  | 'stopping'
  | 'working'
  | 'delegated'
  | 'idle'

export interface PillActivity {
  kind: PillActivityKind
  /** Present on `tool` / `thinking`: the clamped shared label. */
  text?: string
}

export interface PillActivityInput {
  /** The slot's live run state (`selectSlotStreamState`). */
  streamState: SlotState
  /** The slot's live status line (`slotStatusDetail[slot]`), `undefined`
   *  before the first status frame. */
  detail: SlotStatusDetail | undefined
  /** The tool call `detail` describes has already returned its output. */
  toolReturned: boolean
  /** The slots frame's own busy flag (main turn OR sub-agents). */
  running: boolean
  /** Sub-agents are running while the main turn is not. */
  delegatedOnly: boolean
  /** The shared label seam — `toolStatusLabel` bound to the user's
   *  `simplifiedToolNames` preference and the UI language. Injected so this
   *  module stays pure and the test can pin what it hands the seam. */
  labelOf: (detail: ToolStatusDetail) => string
}

/**
 * Cut `text` to at most `max` code points, ending in a single ellipsis when
 * anything was dropped. Code points, not UTF-16 units, so a CJK or emoji
 * purpose is never split through a surrogate pair. Whitespace is collapsed
 * first: a purpose written over two lines is one line here. A cut that lands
 * on a space drops it before the ellipsis, so the result can be one code
 * point under `max`, never over.
 */
export function clampActivityText(text: string, max: number = PILL_ACTIVITY_MAX_CHARS): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  const points = Array.from(flat)
  if (points.length <= max) return flat
  return points.slice(0, Math.max(0, max - 1)).join('').trimEnd() + '…'
}

const THINKING: ToolStatusDetail = { kind: 'thinking' }

/** Resolve the one line the pill shows from the slot's live readings. */
export function resolvePillActivity(input: PillActivityInput): PillActivity {
  const { streamState, detail, toolReturned, running, delegatedOnly, labelOf } = input
  // The run-state phases the status seam has no word for.
  if (streamState === 'compacting') return { kind: 'compacting' }
  if (streamState === 'stopping') return { kind: 'stopping' }
  // Busy is either signal: the slots frame's flag or a run state that has
  // not settled. A stale status from the last turn is never painted on a
  // resting slot.
  const busy = running || streamState !== 'idle'
  if (!busy) return { kind: 'idle' }
  switch (detail?.kind) {
    case 'tool': {
      if (toolReturned) return { kind: 'thinking', text: clampActivityText(labelOf(THINKING)) }
      const label = labelOf(detail)
      return label
        ? { kind: 'tool', text: clampActivityText(label) }
        : { kind: 'thinking', text: clampActivityText(labelOf(THINKING)) }
    }
    case 'thinking':
      return { kind: 'thinking', text: clampActivityText(labelOf(detail)) }
    case 'streaming':
      return { kind: 'writing' }
    default:
      return { kind: delegatedOnly ? 'delegated' : 'working' }
  }
}
