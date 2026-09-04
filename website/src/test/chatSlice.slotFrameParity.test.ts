import { describe, it, expect } from 'vitest'
import reducer, { sseChatMessage, warmSlotCache } from '../store/chatSlice'
import type { ChatMessage } from '../types'
import './mockApiClient'

/**
 * Contract test for the two hand-synced chat-frame appliers. The active-slot
 * path (`sseChatMessage`) and the non-active grid-pane path
 * (`applyNonActiveFrame`, reached by dispatching `sseChatMessage` for a slot
 * that is NOT the focused slot) must shape a representative frame sequence into
 * IDENTICAL message arrays. This pins the two paths so a future frame-kind
 * change to only one is caught by CI — e.g. a thinking-drop divergence where
 * the non-active path drops ALL thinking blocks while the active path drops
 * only the empty placeholder.
 */

const SLOT = 'grid-slot'
const OTHER = 'other-active-slot'
const init = () => reducer(undefined, { type: '@@INIT' })

// role + content is the rendered-array contract. cls / ts / meta and the
// root-vs-per-slot bookkeeping (toolLog, slotState) legitimately differ between
// the two paths and are out of scope for this parity check.
const shape = (msgs: ChatMessage[]) => msgs.map((m) => ({ role: m.role, content: m.content }))

// A representative turn: user -> thinking placeholder -> chunk -> tool -> chunk
// -> permission -> _segment -> _done. The SAME payloads are fed to both paths.
const FRAMES: Array<Parameters<typeof sseChatMessage>[0]> = [
  { slot: SLOT, role: 'user', content: 'summarize the logs' },
  { slot: SLOT, role: 'thinking', content: '' },
  { slot: SLOT, role: 'chunk', content: 'Here', seq: 1 },
  { slot: SLOT, role: 'chunk', content: ' is', seq: 2 },
  { slot: SLOT, role: 'tool', content: '🔧 read', meta: { tool_call_id: 't1' } },
  { slot: SLOT, role: 'chunk', content: ' the answer', seq: 3 },
  { slot: SLOT, role: 'permission', content: 'allow read?', cls: JSON.stringify({ request_id: 'r1', tool_input: 'x' }) },
  { slot: SLOT, role: '_segment', content: '' },
  { slot: SLOT, role: '_done', content: '' },
]

describe('active vs non-active chat-frame applier parity', () => {
  it('shapes a representative frame sequence into identical message arrays', () => {
    // Active: SLOT is the focused slot -> frames build state.messages.
    let active = { ...init(), activeSlot: SLOT }
    for (const f of FRAMES) active = reducer(active, sseChatMessage(f))

    // Non-active: a DIFFERENT focused slot -> the same SLOT frames route through
    // applyNonActiveFrame into slotMessages[SLOT].
    let bg = { ...init(), activeSlot: OTHER }
    for (const f of FRAMES) bg = reducer(bg, sseChatMessage(f))

    expect(shape(bg.slotMessages[SLOT] ?? [])).toEqual(shape(active.messages))
  })

  it('keeps a content-bearing thinking block on the next chunk (both paths)', () => {
    const reasoning: ChatMessage = { role: 'thinking', content: 'prior reasoning', cls: '' }

    let active = { ...init(), activeSlot: SLOT, messages: [reasoning] }
    active = reducer(active, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'x', seq: 1 }))
    expect(active.messages.some((m) => m.role === 'thinking' && m.content === 'prior reasoning')).toBe(true)

    let bg = { ...init(), activeSlot: OTHER, slotMessages: { [SLOT]: [reasoning] } }
    bg = reducer(bg, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'x', seq: 1 }))
    expect((bg.slotMessages[SLOT] ?? []).some((m) => m.role === 'thinking' && m.content === 'prior reasoning')).toBe(true)
  })

  it('drops the empty thinking placeholder on the next chunk (both paths)', () => {
    const placeholder: ChatMessage = { role: 'thinking', content: '', cls: '' }

    let active = { ...init(), activeSlot: SLOT, messages: [placeholder] }
    active = reducer(active, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'x', seq: 1 }))
    expect(active.messages.some((m) => m.role === 'thinking')).toBe(false)

    let bg = { ...init(), activeSlot: OTHER, slotMessages: { [SLOT]: [placeholder] } }
    bg = reducer(bg, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'x', seq: 1 }))
    expect((bg.slotMessages[SLOT] ?? []).some((m) => m.role === 'thinking')).toBe(false)
  })

  it('warmSlotCache reconciles a background pane to canonical history and idles its run state', () => {
    // A non-active pane accrued an optimistic user bubble + streamed content.
    let bg = { ...init(), activeSlot: OTHER }
    bg = reducer(bg, sseChatMessage({ slot: SLOT, role: 'user', content: 'hi' }))
    bg = reducer(bg, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'stream', seq: 1 }))
    expect(bg.slotRun[SLOT]?.state).toBe('streaming')

    // Server canonical history at end-of-turn collapses optimistic + streamed to truth.
    const canonical: ChatMessage[] = [
      { role: 'user', content: 'hi', cls: 'msg msg-u' },
      { role: 'assistant', content: 'streamed answer', cls: 'msg msg-a' },
    ]
    bg = reducer(bg, { type: warmSlotCache.fulfilled.type, payload: { key: SLOT, messages: canonical } })

    expect((bg.slotMessages[SLOT] ?? []).map((m) => ({ role: m.role, content: m.content }))).toEqual(
      canonical.map((m) => ({ role: m.role, content: m.content })),
    )
    expect(bg.slotRun[SLOT]?.state).toBe('idle')
  })
})
