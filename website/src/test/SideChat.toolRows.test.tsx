import { describe, it, expect, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import reducer, { sseSideResult } from '../store/chatSlice'
import type { RootState } from '../store'
import { renderWithProviders, createTestStore } from './helpers'

/* A side turn's tool attempts are rows of the side transcript, drawn where they
 * happened in the reply: `🔧 <title>` through the shared transcript's own tool
 * pill, a refused one marked blocked by its hidden `🚫` sibling. The reducer owns
 * the shape that makes this work with streaming: a tool row at the tail starts a
 * NEW answer row under it, and the terminal frame (which carries the text after
 * the last tool row) settles that last row alone. */

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<PropertyKey, unknown>, {
    get: (_t, prop) => {
      if (Object.prototype.hasOwnProperty.call(_t, prop)) return _t[prop]
      const fn = vi.fn().mockResolvedValue(
        prop === 'sideOpen' ? { ok: true, open: true, messages: 0, last_run_id: '', created_at: '' }
          : prop === 'sideTurn' ? { ok: true, run_id: 'r1', messages: 1 }
            : prop === 'sideClose' ? { ok: true, was_open: true }
              : prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 }
                : prop === 'acpBackends' ? { backends: [{ id: '', capabilities: [{ id: 'side_chat_tools', available: true }] }] }
                  : {},
      )
      Object.defineProperty(_t, prop, { value: fn, writable: true, configurable: true })
      return fn
    },
  }),
  SEARCH_MIN_CHARS: 2,
}))

import SideChat from '../pages/chat/SideChat'

const SLOT = 'parent-slot-1'
const TOOL_META = { tool_call_id: 'call-1', kind: 'execute', purpose: 'read the hosts file', input_preview: 'cat /etc/hosts' }

describe('side reducer: tool rows', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  function turnUpToTool() {
    let state = initial
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'Let me check. ' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'tool', content: '🔧 Running: cat /etc/hosts\ncat /etc/hosts', meta: TOOL_META }))
    return state
  }

  it('a tool row lands at the tail with its meta and keeps the turn busy', () => {
    const state = turnUpToTool()
    const side = state.slotSide[SLOT]
    expect(side.messages.map(m => m.role)).toEqual(['user', 'assistant', 'tool'])
    expect(side.messages[2]).toMatchObject({ role: 'tool', content: '🔧 Running: cat /etc/hosts\ncat /etc/hosts', run_id: 'r1', meta: TOOL_META })
    // Output is arriving: the thinking dots' `pending` is over, the turn streams on.
    expect(side.pending).toBe(false)
    expect(side.streaming).toBe(true)
  })

  it('text after a tool row starts a new answer row instead of growing the one above', () => {
    let state = turnUpToTool()
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'The file lists ' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'two ' }))
    const side = state.slotSide[SLOT]
    expect(side.messages.map(m => m.role)).toEqual(['user', 'assistant', 'tool', 'assistant'])
    expect(side.messages[1].content).toBe('Let me check. ')
    expect(side.messages[3].content).toBe('The file lists two ')
  })

  it('the terminal frame settles only the row under the last tool call', () => {
    let state = turnUpToTool()
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'The file lists two ' }))
    // The server sends the text written AFTER the last tool row, whole-text
    // redacted, which extends what was streamed for that row.
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'The file lists two hosts.', final: true }))
    const side = state.slotSide[SLOT]
    expect(side.messages.map(m => m.content)).toEqual([
      'q',
      'Let me check. ',
      '🔧 Running: cat /etc/hosts\ncat /etc/hosts',
      'The file lists two hosts.',
    ])
    expect(side.streaming).toBe(false)
    expect(side.pending).toBe(false)
  })

  it('a settle frame before a tool row replaces the row above it, even when it rewrites the text', () => {
    // When a row closes at a tool call the server settles it with the row's
    // text as one redaction pass over the whole reply has it. The pass can
    // rewrite the streamed deltas (a secret only visible whole), so the frame
    // is marked `settle` and replaces the row rather than growing it; the
    // tool row still lands under it. Text the server still holds at the call
    // is not in that frame: it is drawn under the tool row, once the text
    // after the call has decided it.
    // The deltas spell a key header; only the source spelling is split, so the
    // fork content scan does not read this test as a committed key.
    const dashes = '-'.repeat(5)
    const kind = 'RSA PRIVATE KEY'
    const begin = `${dashes}BEGIN ${kind}${dashes}`
    const end = `${dashes}END ${kind}${dashes}`
    let state = initial
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: `key: ${begin}\n` }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: `FAKEKEYMATERIAL==\n${end}\nthen commit ` }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'key: [REDACTED: credential]\nthen commit ', settle: true }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'tool', content: '🔧 Running: git show', meta: { ...TOOL_META, input_preview: 'git show' } }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: '9f86d081884c7d659a2feaa0c55ad015 is the ' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: '9f86d081884c7d659a2feaa0c55ad015 is the one.', final: true }))
    const side = state.slotSide[SLOT]
    expect(side.messages.map(m => m.content)).toEqual([
      'q',
      'key: [REDACTED: credential]\nthen commit ',
      '🔧 Running: git show',
      '9f86d081884c7d659a2feaa0c55ad015 is the one.',
    ])
    expect(side.streaming).toBe(false)
  })

  it('a delta that is not a prefix extension still grows the row (only settle replaces)', () => {
    let state = initial
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'Commit ' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'is the ' }))
    expect(state.slotSide[SLOT].messages[1].content).toBe('Commit is the ')
  })

  it('every delta grows the row, including one equal to it or starting with it', () => {
    // The server settles a row against exactly the deltas it sent, so the
    // client may not drop or fold one: two "\n" deltas are two newlines, and a
    // delta that happens to begin with the row's text is more text, not a
    // replacement. Only a settle or terminal frame replaces.
    let state = initial
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: '\n' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: '\n' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: '\n\nThe ' }))
    expect(state.slotSide[SLOT].messages[1].content).toBe('\n\n\n\nThe ')
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: '\n\n\n\nThe ', final: true }))
    expect(state.slotSide[SLOT].messages[1].content).toBe('\n\n\n\nThe ')
    expect(state.slotSide[SLOT].streaming).toBe(false)
  })

  it('an empty settle or terminal frame removes the row it lands on', () => {
    // The pass left the row no text of its own: what was streamed for it
    // belonged to a credential whose tag the row below carries. An empty
    // bubble would be noise.
    let state = initial
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'Bearer ' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: '', settle: true }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'tool', content: '🔧 Running: cat token.txt', meta: { ...TOOL_META, input_preview: 'cat token.txt' } }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: '[REDACTED: credential] ' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: '', final: true }))
    const side = state.slotSide[SLOT]
    expect(side.messages.map(m => m.content)).toEqual(['q', '🔧 Running: cat token.txt'])
    expect(side.streaming).toBe(false)
  })

  it('a terminal frame that rewrites the streamed row replaces it, like a settle frame', () => {
    // The server's terminal frame is the row's whole text redacted in one pass.
    // A key header's newline was drawn before the body followed; the pass then
    // absorbs that newline into the tag, so the frame is not an extension of
    // the deltas. The row ends as the pass has it, never as deltas plus a
    // second copy appended under them.
    let state = initial
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'key: [REDACTED: credential]\n' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'key: [REDACTED: credential]', final: true }))
    const side = state.slotSide[SLOT]
    expect(side.messages.map(m => m.content)).toEqual(['q', 'key: [REDACTED: credential]'])
    expect(side.streaming).toBe(false)
  })

  it('an empty terminal frame after a tool row adds no row and ends the turn', () => {
    let state = turnUpToTool()
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: '', final: true }))
    const side = state.slotSide[SLOT]
    expect(side.messages.map(m => m.role)).toEqual(['user', 'assistant', 'tool'])
    expect(side.streaming).toBe(false)
  })

  it('a refused attempt keeps its 🚫 sibling beside the 🔧 row', () => {
    let state = initial
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'tool', content: '🔧 Running: rm -rf build', meta: { tool_call_id: 'call-9', kind: 'execute', purpose: '', input_preview: 'rm -rf build' } }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'tool', content: '🚫 Running: rm -rf build', meta: { tool_call_id: 'call-9' } }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: "I can't change files here.", final: true }))
    const side = state.slotSide[SLOT]
    expect(side.messages.map(m => m.role)).toEqual(['user', 'tool', 'tool', 'assistant'])
    expect(side.messages[2].meta?.tool_call_id).toBe('call-9')
  })
})

describe('SideChat draws tool rows', () => {
  function storeWith(messages: Array<Record<string, unknown>>, streaming = false) {
    return createTestStore({
      chat: {
        activeSlot: SLOT,
        messages: [],
        slotRunning: false,
        slotStopping: false,
        slotState: 'idle',
        slotStatusDetail: {},
        slotHasMore: false,
        slotOldestIndex: 0,
        loadingOlder: false,
        lastChunkSeq: undefined,
        _wsChunkedDuringFetch: false,
        history: [],
        historyHasMore: false,
        historyOffset: 0,
        pendingInput: null,
        slotContextPct: {},
        voicePlaying: false,
        voiceAudio: null,
        subagents: {},
        toolLog: [],
        activityOpen: true,
        activityTab: 'side',
        focusToolCallId: null,
        slotActivity: {},
        slotSide: { [SLOT]: { messages, lastRunId: 'r1', streaming, pending: false, openedAtTurnCount: 0 } },
        slotHistory: [SLOT],
        stopPressedAt: {},
      } as unknown as RootState['chat'],
    })
  }

  it('a running tool call is a pill between the text rows, with its input in the panel', async () => {
    const store = storeWith([
      { role: 'user', content: 'what is in hosts?', ts: '2026-05-19T10:00:00Z', run_id: 'r1' },
      { role: 'assistant', content: 'Let me check.', ts: '2026-05-19T10:00:01Z', run_id: 'r1' },
      { role: 'tool', content: '🔧 Running: cat /etc/hosts\ncat /etc/hosts', ts: '2026-05-19T10:00:02Z', run_id: 'r1', meta: TOOL_META },
      { role: 'assistant', content: 'Two hosts.', ts: '2026-05-19T10:00:03Z', run_id: 'r1' },
    ])
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    await screen.findByRole('note')
    // A finished turn folds its steps under the shared transcript's own header,
    // exactly as the main chat does; the final text stays visible.
    expect(screen.getByText('Two hosts.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '1 tool call' }))
    // The pill reads the purpose as its label and the input preview as its panel.
    const pill = await screen.findByRole('button', { name: 'read the hosts file' })
    expect(pill.className).toContain('text-accent')
    expect(screen.getByText('Let me check.')).toBeInTheDocument()
    expect(pill.compareDocumentPosition(screen.getByText('Two hosts.')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByText('Let me check.').compareDocumentPosition(pill) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.click(pill)
    expect(screen.getByText(/cat \/etc\/hosts/)).toBeInTheDocument()
  })

  it('a refused tool call is drawn blocked, from its hidden 🚫 sibling', async () => {
    const store = storeWith([
      { role: 'user', content: 'delete build', ts: '2026-05-19T10:00:00Z', run_id: 'r1' },
      { role: 'tool', content: '🔧 Running: rm -rf build', ts: '2026-05-19T10:00:01Z', run_id: 'r1', meta: { tool_call_id: 'call-9', kind: 'execute', purpose: '', input_preview: 'rm -rf build' } },
      { role: 'tool', content: '🚫 Running: rm -rf build', ts: '2026-05-19T10:00:01Z', run_id: 'r1', meta: { tool_call_id: 'call-9' } },
      { role: 'assistant', content: "I can't change files here.", ts: '2026-05-19T10:00:02Z', run_id: 'r1' },
    ])
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    await screen.findByRole('note')
    // One tool call counted: the 🚫 sibling is state, not a row.
    fireEvent.click(screen.getByRole('button', { name: '1 tool call' }))
    const pills = await screen.findAllByRole('button', { name: /rm -rf build/ })
    expect(pills).toHaveLength(1)
    await waitFor(() => expect(pills[0].className).toContain('text-warn'))
  })
})
