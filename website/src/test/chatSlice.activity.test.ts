/**
 * chatSlice: agent activity -- subagent cards, the activity viewer, the tool
 * log, permission rows and how approvals resolve, including a steered message
 * next to a pending approval. The core reducers are in chatSlice.test.ts.
 */
import { describe, it, expect } from 'vitest'
import type { RootState } from '../store'
import reducer, {
  TOOL_OUTPUT_MAX_CHARS,
  clampToolOutput,
  setActiveSlot,
  appendMessage,
  appendSlotMessage,
  sseChatMessage,
  sseSubagentPending,
  sseSubagentSpawn,
  sseSubagentBatchChunks,
  sseSubagentTool,
  sseSubagentDone,
  sseSubagentSnapshot,
  sseSubagentRetrying,
  sseToolActivity,
  sseToolResult,
  sseActivityEvent,
  sseChatMessageUpdate,
  toggleActivity,
  openActivityPanel,
  openActivityToTab,
  switchSlot,
  resolveByApprovalId,
  selectSlotSubagentsActive,
  selectSlotPendingSpawnApprovals,
  selectSlotPendingApproval,
} from '../store/chatSlice'
import './mockApiClient'

describe('subagent reducers', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withSlot = { ...initial, activeSlot: 'slot-1' }

  it('sseSubagentPending creates pending entry', () => {
    const state = reducer(withSlot, sseSubagentPending({ slot: 'slot-1', id: 'a1', task: 'do stuff', approval_id: 'spawn:a1' }))
    expect(state.subagents['a1']).toBeDefined()
    expect(state.subagents['a1'].status).toBe('pending')
    expect(state.subagents['a1'].approval_id).toBe('spawn:a1')
  })

  it('sseSubagentPending ignores wrong slot', () => {
    const state = reducer(withSlot, sseSubagentPending({ slot: 'other', id: 'a1', task: 'do stuff', approval_id: 'spawn:a1' }))
    expect(state.subagents['a1']).toBeUndefined()
  })

  it('sseSubagentSpawn creates running entry', () => {
    const state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'search code', agent: 'amzn-builder' }))
    expect(state.subagents['a1'].status).toBe('running')
    expect(state.subagents['a1'].agent).toBe('amzn-builder')
    expect(state.subagents['a1'].task).toBe('search code')
  })

  it('sseSubagentSpawn carries the resolved model, and later frames never blank a known model (#3582)', () => {
    // Spawn stamps the served model.
    let state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 't', agent: 'kirocrew', model: 'claude-opus-4.8' }))
    expect(state.subagents['a1'].model).toBe('claude-opus-4.8')
    // A tool frame (no model field) must not clobber it.
    state = reducer(state, sseSubagentTool({ slot: 'slot-1', id: 'a1', tool: 'grep' }))
    expect(state.subagents['a1'].model).toBe('claude-opus-4.8')
    // The done frame is authoritative and may refine it (CC path resolved late).
    state = reducer(state, sseSubagentDone({ slot: 'slot-1', id: 'a1', elapsed: 1, outcome: 'completed', model: 'claude-opus-4.7' }))
    expect(state.subagents['a1'].model).toBe('claude-opus-4.7')
    // A done frame WITHOUT a model must not blank a known one.
    let s2 = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a2', task: 't', agent: 'kirocrew', model: 'gpt-5.6-sol' }))
    s2 = reducer(s2, sseSubagentDone({ slot: 'slot-1', id: 'a2', elapsed: 1, outcome: 'completed' }))
    expect(s2.subagents['a2'].model).toBe('gpt-5.6-sol')
  })

  it('sseSubagentSpawn defaults model to empty when the frame omits it', () => {
    const state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 't', agent: '' }))
    expect(state.subagents['a1'].model).toBe('')
  })

  it('sseSubagentSnapshot restores the model on reconnect', () => {
    const state = reducer(withSlot, sseSubagentSnapshot({
      id: 'a1', slot: 'slot-1', task: 't', agent: 'kirocrew', model: 'claude-opus-4.8',
      streaming: '', last_tool: '', started: 1,
    }))
    expect(state.subagents['a1'].model).toBe('claude-opus-4.8')
  })

  it('sseSubagentSnapshot preserves a live retrying flag on reconnect (#7472-adjacent)', () => {
    // A subagent_retrying frame set retrying=true on a still-running card; a
    // reconnect replay snapshot must not blank the ⟳ cue (it carries no attempt
    // field, so it can only preserve, never set, retrying).
    let state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 't', agent: 'kirocrew' }))
    state = reducer(state, sseSubagentRetrying({ slot: 'slot-1', id: 'a1', attempt: 1 }))
    expect(state.subagents['a1'].retrying).toBe(true)
    state = reducer(state, sseSubagentSnapshot({
      id: 'a1', slot: 'slot-1', task: 't', agent: 'kirocrew',
      streaming: '', last_tool: '', started: 1,
    }))
    expect(state.subagents['a1'].retrying).toBe(true)
  })

  it('sseSubagentSpawn preserves existing streaming text from pending', () => {
    let state = reducer(withSlot, sseSubagentPending({ slot: 'slot-1', id: 'a1', task: 'task', approval_id: 'spawn:a1' }))
    state = reducer(state, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'task', agent: 'kirocrew' }))
    expect(state.subagents['a1'].status).toBe('running')
    expect(state.subagents['a1'].startedAt).toBeDefined()
  })

  it('sseSubagentSpawn ignores wrong slot', () => {
    const state = reducer(withSlot, sseSubagentSpawn({ slot: 'other', id: 'a1', task: 'task', agent: '' }))
    expect(state.subagents['a1']).toBeUndefined()
  })

  it('sseSubagentBatchChunks appends streaming text', () => {
    let state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'task', agent: '' }))
    state = reducer(state, sseSubagentBatchChunks({ chunks: [{ slot: 'slot-1', id: 'a1', text: 'hello ' }] }))
    state = reducer(state, sseSubagentBatchChunks({ chunks: [{ slot: 'slot-1', id: 'a1', text: 'world' }] }))
    expect(state.subagents['a1'].streaming).toBe('hello world')
  })

  it('sseSubagentBatchChunks recovers an unknown agent rather than dropping its text', () => {
    // A chunk frame is part of the only evidence the panel gets between one
    // spawn frame and one done frame, so it mints the entry it needs instead of
    // discarding the text. The minted start time is flagged as assumed.
    const state = reducer(withSlot, sseSubagentBatchChunks({ chunks: [{ slot: 'slot-1', id: 'unknown', text: 'data' }] }))
    expect(state.subagents['unknown'].streaming).toBe('data')
    expect(state.subagents['unknown'].startedAtAssumed).toBe(true)
  })

  it('sseSubagentTool updates lastTool and status', () => {
    let state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'task', agent: '' }))
    state = reducer(state, sseSubagentTool({ slot: 'slot-1', id: 'a1', tool: 'grep' }))
    expect(state.subagents['a1'].lastTool).toBe('grep')
    expect(state.subagents['a1'].status).toBe('tool')
  })

  it('sseSubagentDone marks as done', () => {
    let state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'task', agent: '' }))
    state = reducer(state, sseSubagentDone({ slot: 'slot-1', id: 'a1', elapsed: 5.2 }))
    expect(state.subagents['a1'].status).toBe('done')
    expect(state.subagents['a1'].elapsed).toBe(5.2)
  })

  it('sseSubagentDone marks as error when error present', () => {
    let state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'task', agent: '' }))
    state = reducer(state, sseSubagentDone({ slot: 'slot-1', id: 'a1', elapsed: 10, error: 'timeout' }))
    expect(state.subagents['a1'].status).toBe('error')
    expect(state.subagents['a1'].error).toBe('timeout')
  })

  it('sseSubagentDone creates entry retroactively if spawn was missed', () => {
    const state = reducer(withSlot, sseSubagentDone({ slot: 'slot-1', id: 'late1', elapsed: 3, task: 'late task', agent: 'kiro-cli' }))
    expect(state.subagents['late1']).toBeDefined()
    expect(state.subagents['late1'].status).toBe('done')
    expect(state.subagents['late1'].task).toBe('late task')
  })

  it('sseSubagentSpawn copies task onto pending card (empty approval-derived task)', () => {
    // Pending card created from a spawn-approval event whose title carried no
    // task text — the later spawn event holds the authoritative task.
    let state = reducer(withSlot, sseSubagentPending({ slot: 'slot-1', id: 'a1', task: '', approval_id: 'spawn:a1' }))
    state = reducer(state, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'scan package X', agent: 'kirocrew' }))
    expect(state.subagents['a1'].status).toBe('running')
    expect(state.subagents['a1'].task).toBe('scan package X')
  })

  it('sseSubagentDone resolves card by id across slots (mis-bucketed card)', () => {
    // Card lives under activeSlot but the done event arrives with a different
    // slot key (e.g. parent session key changed after reset) — the card must
    // still transition to done instead of staying stuck "running" forever.
    let state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'task', agent: '' }))
    state = reducer(state, sseSubagentDone({ slot: 'other-slot', id: 'a1', elapsed: 7 }))
    expect(state.subagents['a1'].status).toBe('done')
    expect(state.subagents['a1'].elapsed).toBe(7)
  })

  it('sseSubagentDone backfills empty task from done payload', () => {
    let state = reducer(withSlot, sseSubagentPending({ slot: 'slot-1', id: 'a1', task: '', approval_id: 'spawn:a1' }))
    state = reducer(state, sseSubagentDone({ slot: 'slot-1', id: 'a1', elapsed: 4, task: 'the real task' }))
    expect(state.subagents['a1'].status).toBe('done')
    expect(state.subagents['a1'].task).toBe('the real task')
  })

  it('switchSlot saves and restores subagents per slot', () => {
    let state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 'task', agent: '' }))
    // Switch away
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'slot-2', requestId: 'r1', requestStatus: 'pending' } })
    expect(state.subagents).toEqual({})
    // Switch back
    state = reducer(state, { type: 'chat/switchSlot/pending', meta: { arg: 'slot-1', requestId: 'r2', requestStatus: 'pending' } })
    expect(state.subagents['a1']).toBeDefined()
    expect(state.subagents['a1'].status).toBe('running')
  })
})

describe('activity viewer reducers', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withSlot = { ...initial, activeSlot: 'slot-1' }

  it('toggleActivity flips activityOpen', () => {
    expect(initial.activityOpen).toBe(false)
    const state = reducer(initial, toggleActivity())
    expect(state.activityOpen).toBe(true)
    expect(reducer(state, toggleActivity()).activityOpen).toBe(false)
  })

  it('counts a view request only when one is actually made', () => {
    // The counter is what tells the side panel's tab strip "focus this view".
    // A chat switch restores the incoming chat's cached activityTab, and that
    // restore must NOT read as a request — otherwise reopening a chat drags
    // focus off the tab the user left it on.
    expect(initial.activityTabRequest).toBe(0)
    const requested = reducer(withSlot, openActivityToTab('subagents'))
    expect(requested.activityTabRequest).toBe(1)
    // Same view asked for twice is two requests: the user may have clicked away
    // in the strip in between, and the second ask must still pull focus back.
    expect(reducer(requested, openActivityToTab('subagents')).activityTabRequest).toBe(2)

    const switched = reducer(requested, switchSlot.pending('req-1', 'slot-2'))
    expect(switched.activityTab).toBe('changes')
    expect(switched.activityTabRequest).toBe(1)
    // Opening the panel without naming a view is not a request either.
    expect(reducer(switched, openActivityPanel()).activityTabRequest).toBe(1)
  })

  it('sseToolActivity adds to toolLog', () => {
    const state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'grep', kind: 'read', purpose: 'search', input_preview: 'pattern' }))
    expect(state.toolLog).toHaveLength(1)
    expect(state.toolLog[0].text).toBe('grep')
    expect(state.toolLog[0].purpose).toBe('search')
  })

  it('sseToolActivity ignores wrong slot', () => {
    const state = reducer(withSlot, sseToolActivity({ slot: 'other', tool: 'grep', kind: 'read', purpose: '', input_preview: '' }))
    expect(state.toolLog).toHaveLength(0)
  })

  it('sseToolResult attaches output to last tool entry', () => {
    let state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'grep', kind: 'read', purpose: 'search', input_preview: 'pattern' }))
    state = reducer(state, sseToolResult({ slot: 'slot-1', output: 'found 3 matches' }))
    expect(state.toolLog).toHaveLength(1)
    expect(state.toolLog[0].output).toBe('found 3 matches')
  })

  it('sseToolResult is noop without prior tool entry', () => {
    const state = reducer(withSlot, sseToolResult({ slot: 'slot-1', output: 'orphan' }))
    expect(state.toolLog).toHaveLength(0)
  })

  it('sseToolResult stores an output at the ceiling verbatim, with no seam', () => {
    let state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'sh', kind: 'execute', purpose: '', input_preview: '' }))
    const exact = 'a'.repeat(TOOL_OUTPUT_MAX_CHARS)
    state = reducer(state, sseToolResult({ slot: 'slot-1', output: exact }))
    expect(state.toolLog[0].output).toBe(exact)
    expect(state.toolLog[0].output_cut).toBeUndefined()
  })

  it('sseToolResult clamps an oversize output to head + tail and records the seam', () => {
    let state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'sh', kind: 'execute', purpose: '', input_preview: '' }))
    const head = 'H'.repeat(60_000)
    const middle = 'M'.repeat(500_000)
    const tail = 'T'.repeat(60_000)
    state = reducer(state, sseToolResult({ slot: 'slot-1', output: head + middle + tail }))
    const out = state.toolLog[0].output ?? ''
    // Bounded, and by a wide margin: the middle is gone.
    expect(out.length).toBeLessThan(TOOL_OUTPUT_MAX_CHARS + 64)
    expect(out).not.toContain('M')
    // Head first, tail last, one newline between.
    expect(out.startsWith('H')).toBe(true)
    expect(out.endsWith('T')).toBe(true)
    // The store holds NO rendered marker: that is a locale string and belongs
    // to the renderer. The seam is structural.
    expect(out).not.toContain('truncated')
    expect(state.toolLog[0].output_cut).toEqual({ at: 48_001, count: 620_000 - 48_000 - 12_000 })
    expect(out[state.toolLog[0].output_cut!.at - 1]).toBe('\n')
  })

  it('sseToolResult clamps the background slot log the same way', () => {
    let state = reducer(withSlot, sseToolActivity({ slot: 'slot-2', tool: 'sh', kind: 'execute', purpose: '', input_preview: '' }))
    state = reducer(state, sseToolResult({ slot: 'slot-2', output: 'Z'.repeat(TOOL_OUTPUT_MAX_CHARS * 4) }))
    const entry = state.slotActivity['slot-2'].toolLog[0]
    expect((entry.output ?? '').length).toBeLessThan(TOOL_OUTPUT_MAX_CHARS + 64)
    expect(entry.output_cut).toEqual({ at: 48_001, count: TOOL_OUTPUT_MAX_CHARS * 4 - 60_000 })
  })

  it('a later result at or under the ceiling clears a stale seam', () => {
    let state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'sh', kind: 'execute', purpose: '', input_preview: '', tool_call_id: 'tc-re' }))
    state = reducer(state, sseToolResult({ slot: 'slot-1', output: 'Z'.repeat(TOOL_OUTPUT_MAX_CHARS * 2), tool_call_id: 'tc-re' }))
    expect(state.toolLog[0].output_cut).toBeDefined()
    state = reducer(state, sseToolResult({ slot: 'slot-1', output: 'short', tool_call_id: 'tc-re' }))
    expect(state.toolLog[0].output).toBe('short')
    // A leftover offset would make the renderer split the new text.
    expect(state.toolLog[0].output_cut).toBeUndefined()
  })

  it('clampToolOutput snaps both cuts to line breaks and counts the elided characters', () => {
    // 17-character lines ("line 00001 passed" + "\n" = 18 per row), so neither
    // 48 000 nor length-12 000 lands on a row boundary.
    const rows = Array.from({ length: 6_000 }, (_, i) => `line ${String(i + 1).padStart(5, '0')} passed`)
    const raw = rows.join('\n')
    expect(raw.length).toBeGreaterThan(TOOL_OUTPUT_MAX_CHARS)
    const { text, cut } = clampToolOutput(raw)
    expect(cut).not.toBeNull()
    const head = text.slice(0, cut!.at - 1)
    const tail = text.slice(cut!.at)
    // The seam sits on its own newline, and every line on either side of it is
    // a whole source row: no mid-line fragment right above or right below.
    expect(text[cut!.at - 1]).toBe('\n')
    expect(head.split('\n').at(-1)).toMatch(/^line \d{5} passed$/)
    expect(tail.split('\n')[0]).toMatch(/^line \d{5} passed$/)
    expect(text.split('\n')[0]).toBe(rows[0])
    expect(text.split('\n').at(-1)).toBe(rows[rows.length - 1])
    expect(text.length).toBeLessThanOrEqual(TOOL_OUTPUT_MAX_CHARS)
    // Head and tail are verbatim slices, and the count is exactly what sits
    // between them.
    expect(raw.startsWith(head)).toBe(true)
    expect(raw.endsWith(tail)).toBe(true)
    expect(cut!.count).toBe(raw.length - head.length - tail.length)
    expect(text).toBe(head + '\n' + tail)
  })

  it('clampToolOutput keeps the raw offsets when a slice has no line break', () => {
    const raw = 'H'.repeat(60_000) + 'T'.repeat(60_000)
    const { text, cut } = clampToolOutput(raw)
    expect(text).toBe('H'.repeat(48_000) + '\n' + 'T'.repeat(12_000))
    expect(cut).toEqual({ at: 48_001, count: 60_000 })
    // A single trailing newline must not empty the tail.
    const oneLine = 'x'.repeat(100_000) + '\n'
    expect(clampToolOutput(oneLine).text.endsWith('\n' + 'x'.repeat(11_999) + '\n')).toBe(true)
  })

  it('clampToolOutput limits line snapping to the window around each raw cut', () => {
    const farBeforeHead = 'H'.repeat(200) + '\n' + 'x'.repeat(119_799)
    const headOut = clampToolOutput(farBeforeHead)
    expect(headOut.text.slice(0, 48_000)).toBe(farBeforeHead.slice(0, 48_000))
    expect(headOut.text[48_000]).toBe('\n')
    expect(headOut.cut).toEqual({ at: 48_001, count: 60_000 })

    const farAfterTail = 'x'.repeat(113_000) + '\n' + 'T'.repeat(6_999)
    const tailOut = clampToolOutput(farAfterTail)
    expect(tailOut.text.endsWith('\n' + farAfterTail.slice(108_000))).toBe(true)
    expect(tailOut.cut?.count).toBe(60_000)
  })

  it('clampToolOutput returns the input untouched under the ceiling', () => {
    expect(clampToolOutput('short')).toEqual({ text: 'short', cut: null })
    const exact = 'a'.repeat(TOOL_OUTPUT_MAX_CHARS)
    expect(clampToolOutput(exact)).toEqual({ text: exact, cut: null })
  })

  it('clampToolOutput materializes the exact clamped text through Array#join', () => {
    const raw = 'H'.repeat(60_000) + 'T'.repeat(60_000)
    const expected = raw.slice(0, 48_000) + '\n' + raw.slice(108_000)
    expect(clampToolOutput(raw).text).toBe(expected)
    expect(clampToolOutput.toString()).toMatch(/\.join\((['"])\1\)/)
  })

  it('sseToolActivity clamps an oversize input_preview on push and on is_update merge', () => {
    const big = 'I'.repeat(TOOL_OUTPUT_MAX_CHARS * 4)
    let state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'sh', kind: 'execute', purpose: '', input_preview: big, tool_call_id: 'tc-in' }))
    expect(state.toolLog[0].input?.length).toBeLessThan(TOOL_OUTPUT_MAX_CHARS + 64)
    expect(state.toolLog[0].input).not.toContain('truncated')
    expect(state.toolLog[0].input_cut).toEqual({ at: 48_001, count: TOOL_OUTPUT_MAX_CHARS * 4 - 60_000 })
    state = reducer(state, sseToolActivity({ slot: 'slot-1', tool: 'sh', kind: 'execute', purpose: '', input_preview: 'J'.repeat(TOOL_OUTPUT_MAX_CHARS * 4), tool_call_id: 'tc-in', is_update: true }))
    expect(state.toolLog).toHaveLength(1)
    expect(state.toolLog[0].input?.length).toBeLessThan(TOOL_OUTPUT_MAX_CHARS + 64)
    expect(state.toolLog[0].input?.startsWith('J')).toBe(true)
    expect(state.toolLog[0].input_cut).toEqual({ at: 48_001, count: TOOL_OUTPUT_MAX_CHARS * 4 - 60_000 })
    // An update that brings the input back under the ceiling drops the seam.
    state = reducer(state, sseToolActivity({ slot: 'slot-1', tool: 'sh', kind: 'execute', purpose: '', input_preview: '{"command":"ls"}', tool_call_id: 'tc-in', is_update: true }))
    expect(state.toolLog[0].input).toBe('{"command":"ls"}')
    expect(state.toolLog[0].input_cut).toBeUndefined()
  })

  it('an unclamped push carries no input_cut key at all', () => {
    const state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'sh', kind: 'execute', purpose: '', input_preview: 'ls' }))
    expect('input_cut' in state.toolLog[0]).toBe(false)
  })

  it('streamed answer chunks are not duplicated into the tool log', () => {
    // Nothing renders a tool-log copy of the answer stream, and one per turn
    // grew without bound on both the active and background paths.
    let state = reducer(withSlot, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'hello ', seq: 1 }))
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'chunk', content: 'world', seq: 2 }))
    expect(state.toolLog).toHaveLength(0)
    expect(state.messages.find(m => m.role === 'streaming')?.content).toBe('hello world')
    state = reducer(state, sseChatMessage({ slot: 'slot-2', role: 'chunk', content: 'background', seq: 1 }))
    expect(state.slotActivity['slot-2']?.toolLog ?? []).toHaveLength(0)
    expect(state.slotMessages['slot-2'].find(m => m.role === 'streaming')?.content).toBe('background')
  })

  it('sseActivityEvent adds system event to toolLog', () => {
    const state = reducer(withSlot, sseActivityEvent({ slot: 'slot-1', kind: 'context', text: 'Injected 5000 chars' }))
    expect(state.toolLog).toHaveLength(1)
    expect(state.toolLog[0].type).toBe('context')
  })

  it('sseActivityEvent ignores wrong slot', () => {
    const state = reducer(withSlot, sseActivityEvent({ slot: 'other', kind: 'context', text: 'data' }))
    expect(state.toolLog).toHaveLength(0)
  })
})

describe('approval_resolved and toolLog mutations', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withSlot = { ...initial, activeSlot: 'slot-1' }

  it('approval_resolved marks matching approval entry as resolved', () => {
    let state = reducer(withSlot, sseActivityEvent({ slot: 'slot-1', kind: 'approval', text: 'spawn_run', approval_id: 'spawn:a1', approval_type: 'spawn' }))
    expect(state.toolLog).toHaveLength(1)
    state = reducer(state, sseActivityEvent({ slot: 'slot-1', kind: 'approval_resolved', text: '', approval_id: 'spawn:a1' }))
    expect(state.toolLog).toHaveLength(1)
    expect(state.toolLog[0].type).toBe('approval_resolved')
  })

  it('approval_resolved only affects matching approval entries', () => {
    let state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'grep', kind: 'read', purpose: 'search', input_preview: 'pattern' }))
    state = reducer(state, sseActivityEvent({ slot: 'slot-1', kind: 'approval', text: 'spawn_run', approval_id: 'spawn:a1', approval_type: 'spawn' }))
    expect(state.toolLog).toHaveLength(2)
    state = reducer(state, sseActivityEvent({ slot: 'slot-1', kind: 'approval_resolved', text: '', approval_id: 'spawn:a1' }))
    expect(state.toolLog).toHaveLength(2)
    expect(state.toolLog[0].type).toBe('tool')
    expect(state.toolLog[1].type).toBe('approval_resolved')
  })

  it('toolLog clears on new user message', () => {
    let state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'grep', kind: 'read', purpose: '', input_preview: '' }))
    state = reducer(state, sseToolActivity({ slot: 'slot-1', tool: 'read', kind: 'read', purpose: '', input_preview: '' }))
    expect(state.toolLog).toHaveLength(2)
    state = reducer(state, sseChatMessage({ slot: 'slot-1', role: 'user', content: 'hello' }))
    expect(state.toolLog).toHaveLength(0)
  })

  it('sseToolResult matches by tool_call_id', () => {
    let state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'grep', kind: 'read', purpose: '', input_preview: '', tool_call_id: 'tc1' }))
    state = reducer(state, sseToolActivity({ slot: 'slot-1', tool: 'read', kind: 'read', purpose: '', input_preview: '', tool_call_id: 'tc2' }))
    state = reducer(state, sseToolResult({ slot: 'slot-1', output: 'grep output', tool_call_id: 'tc1' }))
    expect(state.toolLog[0].output).toBe('grep output')
    expect(state.toolLog[1].output).toBeUndefined()
  })

  it('sseToolActivity is_update merges into existing entry by tool_call_id', () => {
    // Simulate the claude-agent-acp two-phase flow: first a stub tool_call,
    // then a tool_call_update with the refined title/input.
    let state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'Terminal', kind: 'execute', purpose: '', input_preview: '', tool_call_id: 'tc-bash-1' }))
    expect(state.toolLog).toHaveLength(1)
    state = reducer(state, sseToolActivity({ slot: 'slot-1', tool: 'List Kiro Crew modules', kind: 'execute', purpose: '', input_preview: '{"command":"ls"}', tool_call_id: 'tc-bash-1', is_update: true }))
    expect(state.toolLog).toHaveLength(1)
    expect(state.toolLog[0].text).toBe('List Kiro Crew modules')
    expect(state.toolLog[0].input).toBe('{"command":"ls"}')
  })

  it('sseToolActivity without is_update does not merge — replayed initial event appends', () => {
    // A duplicate initial tool_call (e.g. WebSocket reconnect/replay) should
    // NOT silently merge into the previous entry. Only is_update events do.
    let state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'Terminal', kind: 'execute', purpose: '', input_preview: '', tool_call_id: 'tc-bash-1' }))
    state = reducer(state, sseToolActivity({ slot: 'slot-1', tool: 'Terminal', kind: 'execute', purpose: '', input_preview: '', tool_call_id: 'tc-bash-1' }))
    expect(state.toolLog).toHaveLength(2)
  })

  it('sseToolActivity is_update with no existing entry falls through to append', () => {
    // If the update arrives before its initial tool_call (out of order, or
    // initial dropped), don't drop it on the floor — append a new row.
    const state = reducer(withSlot, sseToolActivity({ slot: 'slot-1', tool: 'ls /tmp', kind: 'execute', purpose: '', input_preview: '', tool_call_id: 'tc-orphan', is_update: true }))
    expect(state.toolLog).toHaveLength(1)
    expect(state.toolLog[0].text).toBe('ls /tmp')
  })

  it('sseChatMessageUpdate patches matching tool message content+meta', () => {
    let state = reducer(withSlot, appendMessage({ role: 'tool', content: '🔧 Terminal', cls: '', meta: { tool_call_id: 'tc-bash-1' } }))
    state = reducer(state, sseChatMessageUpdate({ slot: 'slot-1', tool_call_id: 'tc-bash-1', content: '🔧 ls /tmp', meta: { input: '{"command":"ls /tmp"}' } }))
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].content).toBe('🔧 ls /tmp')
    expect(state.messages[0].meta?.input).toBe('{"command":"ls /tmp"}')
    expect(state.messages[0].meta?.tool_call_id).toBe('tc-bash-1')
  })

  it('sseChatMessageUpdate walks reverse and stops at most-recent match', () => {
    // Two tool messages can share a tool_call_id (auto-approved tools emit
    // 🔧 + ✅). The reducer should patch the most recent (the ✅).
    let state = reducer(withSlot, appendMessage({ role: 'tool', content: '🔧 Terminal', cls: '', meta: { tool_call_id: 'tc-bash-1' } }))
    state = reducer(state, appendMessage({ role: 'tool', content: '✅ Terminal', cls: '', meta: { tool_call_id: 'tc-bash-1' } }))
    state = reducer(state, sseChatMessageUpdate({ slot: 'slot-1', tool_call_id: 'tc-bash-1', content: '✅ ls /tmp' }))
    expect(state.messages[0].content).toBe('🔧 Terminal')
    expect(state.messages[1].content).toBe('✅ ls /tmp')
  })

  it('sseChatMessageUpdate is no-op when slot mismatches active', () => {
    let state = reducer(withSlot, appendMessage({ role: 'tool', content: '🔧 Terminal', cls: '', meta: { tool_call_id: 'tc-bash-1' } }))
    state = reducer(state, sseChatMessageUpdate({ slot: 'other', tool_call_id: 'tc-bash-1', content: '🔧 ls /tmp' }))
    expect(state.messages[0].content).toBe('🔧 Terminal')
  })

  it('sseChatMessageUpdate is no-op when tool_call_id is missing', () => {
    let state = reducer(withSlot, appendMessage({ role: 'tool', content: '🔧 Terminal', cls: '', meta: { tool_call_id: 'tc-bash-1' } }))
    state = reducer(state, sseChatMessageUpdate({ slot: 'slot-1', tool_call_id: '', content: '🔧 changed' }))
    expect(state.messages[0].content).toBe('🔧 Terminal')
  })
})

describe('permission cls parsing and approval resolution', () => {
  const slot = 'test-slot'
  const mkState = () => {
    let s = reducer(undefined, { type: '@@INIT' })
    s = reducer(s, setActiveSlot(slot))
    return s
  }

  it('parses cls JSON into meta.approval_id on permission messages', () => {
    const cls = JSON.stringify({ request_id: 'req-42', tool_input: 'echo hi', is_read_only: 'true' })
    const state = reducer(mkState(), sseChatMessage({ slot, role: 'permission', content: 'approve?', cls }))
    const msg = state.messages[0]
    expect(msg.role).toBe('permission')
    expect(msg.meta?.approval_id).toBe('req-42')
    expect(msg.meta?.tool_input).toBe('echo hi')
    expect(msg.meta?.is_read_only).toBe('true')
  })

  it('ignores non-JSON cls gracefully', () => {
    const state = reducer(mkState(), sseChatMessage({ slot, role: 'permission', content: 'approve?', cls: 'not-json' }))
    expect(state.messages[0].meta?.approval_id).toBeUndefined()
  })

  it('preserves existing meta when cls has no request_id', () => {
    const cls = JSON.stringify({ description: 'some tool' })
    const state = reducer(mkState(), sseChatMessage({ slot, role: 'permission', content: 'approve?', cls, meta: { custom: 'val' } }))
    expect(state.messages[0].meta?.custom).toBe('val')
    expect(state.messages[0].meta?.approval_id).toBeUndefined()
  })

  it('skips cls parsing when meta already has approval_id', () => {
    const cls = JSON.stringify({ request_id: 'req-new' })
    const state = reducer(mkState(), sseChatMessage({ slot, role: 'permission', content: 'approve?', cls, meta: { approval_id: 'req-existing' } }))
    expect(state.messages[0].meta?.approval_id).toBe('req-existing')
  })

  it('resolveByApprovalId marks permission as approved', () => {
    const cls = JSON.stringify({ request_id: 'req-1' })
    let state = reducer(mkState(), sseChatMessage({ slot, role: 'permission', content: 'approve?', cls }))
    state = reducer(state, resolveByApprovalId({ slot, id: 'req-1', decision: 'approved' }))
    expect(state.messages[0].meta?.resolved).toBe('approved')
  })

  it('resolveByApprovalId marks permission as rejected', () => {
    const cls = JSON.stringify({ request_id: 'req-2' })
    let state = reducer(mkState(), sseChatMessage({ slot, role: 'permission', content: 'approve?', cls }))
    state = reducer(state, resolveByApprovalId({ slot, id: 'req-2', decision: 'rejected' }))
    expect(state.messages[0].meta?.resolved).toBe('rejected')
  })

  it('resolveByApprovalId is no-op for unknown id', () => {
    const cls = JSON.stringify({ request_id: 'req-3' })
    let state = reducer(mkState(), sseChatMessage({ slot, role: 'permission', content: 'approve?', cls }))
    state = reducer(state, resolveByApprovalId({ slot, id: 'req-unknown' }))
    expect(state.messages[0].meta?.resolved).toBeUndefined()
  })

  it('resolveByApprovalId does not downgrade a decided row to stale', () => {
    // A stale retirement carries no outcome (expired wait, 404, or a reconcile
    // snapshot that no longer lists the id). Arriving after a real decision it
    // is old news, not a new fact, and must leave the decision in place.
    const cls = JSON.stringify({ request_id: 'req-decided' })
    let state = reducer(mkState(), sseChatMessage({ slot, role: 'permission', content: 'approve?', cls }))
    state = reducer(state, resolveByApprovalId({ slot, id: 'req-decided', decision: 'approved' }))
    state = reducer(state, resolveByApprovalId({ slot, id: 'req-decided', decision: 'stale' }))
    expect(state.messages[0].meta?.resolved).toBe('approved')
  })

  it('resolveByApprovalId lets a real decision overwrite stale on the selected registry', () => {
    // The guard is directional: a decision that lands after a stale marker is
    // the outcome the card was waiting for, so that direction stays open. A
    // registry-qualified resolution must update the matching collision row.
    let state = reducer(mkState(), sseChatMessage({
      slot, role: 'permission', content: 'runner?', meta: { approval_id: 'req-late' },
    }))
    state = reducer(state, sseChatMessage({
      slot, role: 'permission', content: 'coordinator?',
      meta: { approval_id: 'req-late', registry: 'coordinator' },
    }))
    state = reducer(state, resolveByApprovalId({
      slot, id: 'req-late', decision: 'stale', registry: 'coordinator',
    }))
    expect(state.messages[0].meta?.resolved).toBeUndefined()
    expect(state.messages[1].meta?.resolved).toBe('stale')
    state = reducer(state, resolveByApprovalId({
      slot, id: 'req-late', decision: 'rejected', registry: 'coordinator',
    }))
    expect(state.messages[0].meta?.resolved).toBeUndefined()
    expect(state.messages[1].meta?.resolved).toBe('rejected')
  })

  it('resolveByApprovalId still marks a pending row stale', () => {
    const cls = JSON.stringify({ request_id: 'req-pending' })
    let state = reducer(mkState(), sseChatMessage({ slot, role: 'permission', content: 'approve?', cls }))
    state = reducer(state, resolveByApprovalId({ slot, id: 'req-pending', decision: 'stale' }))
    expect(state.messages[0].meta?.resolved).toBe('stale')
  })

  it('resolved permission is filterable by meta.resolved', () => {
    const cls1 = JSON.stringify({ request_id: 'req-a' })
    const cls2 = JSON.stringify({ request_id: 'req-b' })
    let state = reducer(mkState(), sseChatMessage({ slot, role: 'permission', content: 'tool1', cls: cls1 }))
    state = reducer(state, sseChatMessage({ slot, role: 'permission', content: 'tool2', cls: cls2 }))
    state = reducer(state, resolveByApprovalId({ slot, id: 'req-a', decision: 'approved' }))
    const pending = state.messages.filter(m => m.role === 'permission' && !m.meta?.resolved)
    expect(pending).toHaveLength(1)
    expect(pending[0].meta?.approval_id).toBe('req-b')
  })
})

describe('selectSlotSubagentsActive', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withSlot = { ...initial, activeSlot: 'slot-1' }
  const wrap = (chat: ReturnType<typeof reducer>) => ({ chat }) as never

  it('is false with no subagents', () => {
    expect(selectSlotSubagentsActive(wrap(withSlot), 'slot-1')).toBe(false)
  })

  it('is true while a subagent runs on the active slot (spawn event)', () => {
    const state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 't', agent: '' }))
    expect(selectSlotSubagentsActive(wrap(state), 'slot-1')).toBe(true)
  })

  it('is true for a pending subagent (awaiting spawn approval)', () => {
    const state = reducer(withSlot, sseSubagentPending({ slot: 'slot-1', id: 'a1', task: 't', approval_id: 'spawn:a1' }))
    expect(selectSlotSubagentsActive(wrap(state), 'slot-1')).toBe(true)
  })

  it('clears when the subagent finishes (done event — reaper self-heal path)', () => {
    let state = reducer(withSlot, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 't', agent: '' }))
    state = reducer(state, sseSubagentDone({ slot: 'slot-1', id: 'a1', elapsed: 1 }))
    expect(selectSlotSubagentsActive(wrap(state), 'slot-1')).toBe(false)
  })

  it('reads background slots from slotActivity, not the active-slot map', () => {
    const state = reducer(withSlot, sseSubagentSpawn({ slot: 'bg-slot', id: 'b1', task: 't', agent: '' }))
    expect(selectSlotSubagentsActive(wrap(state), 'bg-slot')).toBe(true)
    expect(selectSlotSubagentsActive(wrap(state), 'slot-1')).toBe(false)
  })
})

describe('selectSlotPendingSpawnApprovals', () => {
  const initial = reducer(undefined, { type: '@@INIT' })
  const withSlot = { ...initial, activeSlot: 'slot-1' }
  const wrap = (chat: typeof initial) => ({ chat }) as unknown as RootState

  it('returns pending spawn approvals for the active slot', () => {
    const state = reducer(withSlot, sseSubagentPending({ slot: 'slot-1', id: 'a1', task: 'do stuff', approval_id: 'spawn:a1' }))
    const pending = selectSlotPendingSpawnApprovals(wrap(state), 'slot-1')
    expect(pending).toHaveLength(1)
    expect(pending[0].id).toBe('a1')
    expect(pending[0].approval_id).toBe('spawn:a1')
  })

  it('is empty (stable ref) when the slot has no pending spawns', () => {
    const a = selectSlotPendingSpawnApprovals(wrap(withSlot), 'slot-1')
    const b = selectSlotPendingSpawnApprovals(wrap(withSlot), 'slot-1')
    expect(a).toHaveLength(0)
    expect(a).toBe(b) // referentially stable so shallowEqual short-circuits re-renders
  })

  it('returns empty for a null slot', () => {
    expect(selectSlotPendingSpawnApprovals(wrap(withSlot), null)).toHaveLength(0)
  })

  it('drops the approval once the sub-agent starts running', () => {
    let state = reducer(withSlot, sseSubagentPending({ slot: 'slot-1', id: 'a1', task: 't', approval_id: 'spawn:a1' }))
    expect(selectSlotPendingSpawnApprovals(wrap(state), 'slot-1')).toHaveLength(1)
    state = reducer(state, sseSubagentSpawn({ slot: 'slot-1', id: 'a1', task: 't', agent: 'kirocrew' }))
    expect(selectSlotPendingSpawnApprovals(wrap(state), 'slot-1')).toHaveLength(0)
  })

  it('surfaces pending spawns parked under a background (non-active) slot', () => {
    // Pending card for a slot the user is NOT currently viewing lands in
    // slotActivity[slot]; the selector must still find it when queried by slot.
    const state = reducer(withSlot, sseSubagentPending({ slot: 'bg-slot', id: 'a2', task: 't', approval_id: 'spawn:a2' }))
    const pending = selectSlotPendingSpawnApprovals(wrap(state), 'bg-slot')
    expect(pending).toHaveLength(1)
    expect(pending[0].id).toBe('a2')
  })
})

describe('steer does not deadlock pending approval (#1667)', () => {
  const slot = 'slot-1'
  const initial = reducer(undefined, { type: '@@INIT' })
  const wrap = (chat: ReturnType<typeof reducer>) => ({ chat }) as never

  // Builds a state with an active slot, a pending permission row, and a toolLog entry.
  const withPendingApproval = () => {
    let state = { ...initial, activeSlot: slot }
    // Inject a permission row (active-slot path via sseChatMessage)
    const cls = JSON.stringify({ request_id: 'req-1', tool_input: 'rm -rf /', is_read_only: '' })
    state = reducer(state, sseChatMessage({ slot, role: 'permission', content: 'approve rm?', cls }))
    // Add a tool activity entry so toolLog is non-empty
    state = reducer(state, sseToolActivity({ slot, tool: 'bash', kind: 'write', purpose: 'delete', input_preview: 'rm -rf' }))
    return state
  }

  describe('sseChatMessage (active-slot path)', () => {
    it('steered user message does NOT auto-resolve pending permission rows', () => {
      let state = withPendingApproval()
      expect(state.messages.find(m => m.role === 'permission')?.meta?.resolved).toBeUndefined()
      state = reducer(state, sseChatMessage({ slot, role: 'user', content: 'also check /tmp', meta: { steer: true } }))
      // Permission must remain unresolved
      expect(state.messages.find(m => m.role === 'permission')?.meta?.resolved).toBeUndefined()
    })

    it('steered user message does NOT clear the toolLog', () => {
      let state = withPendingApproval()
      expect(state.toolLog.length).toBeGreaterThan(0)
      state = reducer(state, sseChatMessage({ slot, role: 'user', content: 'also check /tmp', meta: { steer: true } }))
      expect(state.toolLog.length).toBeGreaterThan(0)
    })

    it('normal user message STILL auto-resolves pending permissions (existing behavior)', () => {
      let state = withPendingApproval()
      state = reducer(state, sseChatMessage({ slot, role: 'user', content: 'new turn' }))
      expect(state.messages.find(m => m.role === 'permission')?.meta?.resolved).toBe('rejected')
    })

    it('normal user message STILL clears the toolLog (existing behavior)', () => {
      let state = withPendingApproval()
      state = reducer(state, sseChatMessage({ slot, role: 'user', content: 'new turn' }))
      expect(state.toolLog).toHaveLength(0)
    })
  })

  describe('applyNonActiveFrame (background-slot path)', () => {
    const bgSlot = 'bg-slot'

    const withBgPendingApproval = () => {
      // Active slot is different from bgSlot so bgSlot hits applyNonActiveFrame
      let state = { ...initial, activeSlot: slot }
      const cls = JSON.stringify({ request_id: 'req-bg', tool_input: 'drop db', is_read_only: '' })
      state = reducer(state, sseChatMessage({ slot: bgSlot, role: 'permission', content: 'approve drop?', cls }))
      return state
    }

    it('steered user message does NOT auto-resolve permission rows in background slot', () => {
      let state = withBgPendingApproval()
      const bgMsgs = () => state.slotMessages[bgSlot] ?? []
      expect(bgMsgs().find(m => m.role === 'permission')?.meta?.resolved).toBeUndefined()
      state = reducer(state, sseChatMessage({ slot: bgSlot, role: 'user', content: 'steer correction', meta: { steer: true } }))
      expect(bgMsgs().find(m => m.role === 'permission')?.meta?.resolved).toBeUndefined()
    })

    it('steered user message does NOT clear the background slot toolLog', () => {
      let state = withBgPendingApproval()
      // Add a tool log entry in the background slot
      state = reducer(state, sseToolActivity({ slot: bgSlot, tool: 'grep', kind: 'read', purpose: '', input_preview: '' }))
      expect(state.slotActivity[bgSlot]?.toolLog.length).toBeGreaterThan(0)
      state = reducer(state, sseChatMessage({ slot: bgSlot, role: 'user', content: 'steer', meta: { steer: true } }))
      expect(state.slotActivity[bgSlot]?.toolLog.length).toBeGreaterThan(0)
    })

    it('normal user message STILL auto-resolves permissions in background slot', () => {
      let state = withBgPendingApproval()
      state = reducer(state, sseChatMessage({ slot: bgSlot, role: 'user', content: 'new turn' }))
      const bgMsgs = state.slotMessages[bgSlot] ?? []
      expect(bgMsgs.find(m => m.role === 'permission')?.meta?.resolved).toBe('rejected')
    })

    it('normal user message STILL clears the background slot toolLog', () => {
      let state = withBgPendingApproval()
      state = reducer(state, sseToolActivity({ slot: bgSlot, tool: 'grep', kind: 'read', purpose: '', input_preview: '' }))
      expect(state.slotActivity[bgSlot]?.toolLog.length).toBeGreaterThan(0)
      state = reducer(state, sseChatMessage({ slot: bgSlot, role: 'user', content: 'new turn' }))
      expect(state.slotActivity[bgSlot]?.toolLog).toHaveLength(0)
    })
  })

  describe('selectSlotPendingApproval ignores steered user messages', () => {
    it('returns the pending approval even after a steered user message is appended', () => {
      let state = withPendingApproval()
      // Selector should find the permission row
      expect(selectSlotPendingApproval(wrap(state), slot)).not.toBeNull()
      expect(selectSlotPendingApproval(wrap(state), slot)?.meta?.approval_id).toBe('req-1')
      // Append a steered user message
      state = reducer(state, sseChatMessage({ slot, role: 'user', content: 'also try X', meta: { steer: true } }))
      // Selector must STILL find the pending permission
      expect(selectSlotPendingApproval(wrap(state), slot)).not.toBeNull()
      expect(selectSlotPendingApproval(wrap(state), slot)?.meta?.approval_id).toBe('req-1')
    })

    it('a normal user message hides the pending approval (existing behavior)', () => {
      let state = withPendingApproval()
      expect(selectSlotPendingApproval(wrap(state), slot)).not.toBeNull()
      state = reducer(state, sseChatMessage({ slot, role: 'user', content: 'new turn' }))
      // The permission gets resolved AND is now before the last user msg
      expect(selectSlotPendingApproval(wrap(state), slot)).toBeNull()
    })

    it('works with appendSlotMessage (optimistic steer bubble)', () => {
      let state = withPendingApproval()
      expect(selectSlotPendingApproval(wrap(state), slot)).not.toBeNull()
      state = reducer(state, appendSlotMessage({ slot, message: { role: 'user', content: 'steer text', cls: 'msg msg-u', meta: { steer: true, optimistic: true } } }))
      // Approval must still be visible
      expect(selectSlotPendingApproval(wrap(state), slot)).not.toBeNull()
      expect(selectSlotPendingApproval(wrap(state), slot)?.meta?.approval_id).toBe('req-1')
    })
  })
})
