/**
 * Tool-row disclosure identity collision regression (#8204).
 *
 * messageRowKey is `${role}-${clientTs ?? ts}` and tool rows are never
 * clientTs-stamped, so a burst of tool rows appended in one server tick all
 * key as `tool-<tick>`. ChatPage used that string as BOTH the React key and
 * the toolDisclosure map key, so expanding one same-tick tool row expanded
 * all of them. The React-key role is not at stake (each renderMessage element
 * is the sole child of a separately keyed wrapper); the load-bearing role is
 * the disclosure map identity — which toolDisclosureKey now disambiguates by
 * folding meta.tool_call_id in when present.
 *
 * Probe adapted from the issue (credit @chenmingwei23): driven through the
 * REAL refreshSlot reducer rather than synthesized state, with per-item
 * pairing assertions — a set-size check alone cannot express cross-attribution.
 */
import { describe, it, expect } from 'vitest'
import reducer, { refreshSlot } from '../store/chatSlice'
import { messageRowKey, toolDisclosureKey } from '../pages/ChatPage'
import type { ChatMessage } from '../types'

const SLOT = 'disclosure-key-slot'
const initial = reducer(undefined, { type: '@@INIT' })
const withSlot = { ...initial, activeSlot: SLOT }

// Payload shape per chatSlice.streamingKeyStability.test.ts
const detailPayload = (key: string, messages: ChatMessage[]) => ({
  key, messages, running: false, stopping: false, hasMore: false, total: messages.length,
  queue: [] as { content: string; queueId: string; ts: string }[], context: undefined,
})

describe('tool-row disclosure identity (#8204)', () => {
  it('gives same-tick tool rows with distinct tool_call_ids distinct disclosure keys', () => {
    const server: ChatMessage[] = [
      { role: 'tool', content: '🔧 alpha', cls: '', ts: 'tick', meta: { tool_call_id: 'tc1' } },
      { role: 'tool', content: '🔧 bravo', cls: '', ts: 'tick', meta: { tool_call_id: 'tc2' } },
      { role: 'tool', content: '🔧 charlie', cls: '', ts: 'tick', meta: { tool_call_id: 'tc3' } },
    ]
    const state = reducer(withSlot, refreshSlot.fulfilled(detailPayload(SLOT, server), 'r1', SLOT))
    const tools = state.messages.filter(m => m.role === 'tool')

    // Controls: the reducer really ingested 3 rows, they carry 3 distinct
    // tool_call_ids, and none stole a clientTs stamp.
    expect(tools.length).toBe(3)
    expect(new Set(tools.map(t => t.meta?.tool_call_id)).size).toBe(3)
    expect(tools.every(t => t.meta?.clientTs === undefined)).toBe(true)

    // NOTE: deliberately no assertion that the row keys THEMSELVES collide —
    // whether messageRowKey ever stops colliding (e.g. tool rows gain a
    // clientTs stamp) is the key-stability suite's contract, not this one's.
    // This suite pins only the property under test: distinct disclosure keys.

    // The fix: per-item pairing — each tool_call_id maps to its OWN disclosure
    // key, and that key embeds the id so the pairing is attributable.
    const pairing = tools.map((t, i) => ({
      tcid: t.meta?.tool_call_id as string,
      dKey: toolDisclosureKey(t, messageRowKey(t, i)),
    }))
    for (const { tcid, dKey } of pairing) {
      expect(dKey).toBe(`tool-tick-${tcid}`)
    }
    expect(new Set(pairing.map(p => p.dKey)).size).toBe(3)
  })

  it('leaves the disclosure key unchanged when a tool row has no tool_call_id', () => {
    // Legacy/replayed tool rows may lack the id; their disclosure identity must
    // stay the row key so nothing about their behavior shifts.
    const m: ChatMessage = { role: 'tool', content: '🔧 legacy', cls: '', ts: 'tick' }
    expect(toolDisclosureKey(m, messageRowKey(m, 0))).toBe('tool-tick')
  })

  it('leaves non-tool roles unchanged (key shape preservation)', () => {
    // Only the tool path is re-keyed: an assistant row carrying a stray
    // tool_call_id (never produced today) still keeps its plain row key, so
    // persisted-in-session disclosure state for every other role is untouched.
    const m: ChatMessage = { role: 'assistant', content: 'a', cls: '', ts: 'tick', meta: { tool_call_id: 'tcX' } }
    expect(toolDisclosureKey(m, messageRowKey(m, 0))).toBe('assistant-tick')
  })

  it('keeps the messageRowKey contract itself untouched for tool rows', () => {
    // The key-stability suite pins messageRowKey(tool) === 'tool-tick' — the
    // fix must not alter messageRowKey, only the disclosure identity built on it.
    const m: ChatMessage = { role: 'tool', content: '🔧 x', cls: '', ts: 'tick', meta: { tool_call_id: 'tc9' } }
    expect(messageRowKey(m, 0)).toBe('tool-tick')
  })
})
