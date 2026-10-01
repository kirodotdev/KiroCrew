import { describe, expect, it, vi } from 'vitest'
import { PILL_ACTIVITY_MAX_CHARS, clampActivityText, resolvePillActivity, type PillActivityInput } from './pillActivity'
import type { ToolStatusDetail } from '../../utils/toolStatusLabel'

/** A stand-in for `toolStatusLabel`: the purpose for a tool status, fixed
 *  copy for the phases — enough to pin what the resolver hands the seam. */
const labelOf = (d: ToolStatusDetail): string => {
  if (d.kind === 'tool') return d.purpose || d.toolName || ''
  if (d.label) return d.label
  return d.kind === 'thinking' ? 'Thinking…' : d.kind === 'streaming' ? 'Streaming' : ''
}

const base: PillActivityInput = {
  streamState: 'idle',
  detail: undefined,
  toolReturned: false,
  running: false,
  delegatedOnly: false,
  labelOf,
}
const tool = (purpose: string, toolName = 'shell'): PillActivityInput['detail'] =>
  ({ kind: 'tool', purpose, toolName, toolCallId: 't1', ts: 1 })

describe('clampActivityText', () => {
  it('returns short text unchanged, whitespace collapsed', () => {
    expect(clampActivityText('  read   the\nfile ')).toBe('read the file')
  })

  it('cuts to the cap in code points and ends in one ellipsis', () => {
    const long = 'x'.repeat(PILL_ACTIVITY_MAX_CHARS + 10)
    const out = clampActivityText(long)
    expect(Array.from(out)).toHaveLength(PILL_ACTIVITY_MAX_CHARS)
    expect(out.endsWith('…')).toBe(true)
    expect(out.split('…')).toHaveLength(2)
  })

  it('a cut that lands on a space drops it, so the result is under the cap, never over', () => {
    // 39 code points "…take a " then "screenshot": the 39th point is a space.
    const text = 'Rebuild the whole site and then take a screenshot of every page in both themes'
    const out = clampActivityText(text)
    expect(out).toBe('Rebuild the whole site and then take a…')
    expect(Array.from(out).length).toBeLessThanOrEqual(PILL_ACTIVITY_MAX_CHARS)
  })

  it('does not cut through a surrogate pair or count one as two', () => {
    const emoji = '🔧'.repeat(PILL_ACTIVITY_MAX_CHARS)
    expect(clampActivityText(emoji)).toBe(emoji)
    const over = '🔧'.repeat(PILL_ACTIVITY_MAX_CHARS + 1)
    expect(clampActivityText(over)).toBe('🔧'.repeat(PILL_ACTIVITY_MAX_CHARS - 1) + '…')
  })

  it('a text exactly at the cap is not cut', () => {
    const exact = 'y'.repeat(PILL_ACTIVITY_MAX_CHARS)
    expect(clampActivityText(exact)).toBe(exact)
  })
})

describe('resolvePillActivity', () => {
  it('a running tool call shows the shared seam label for that status, clamped', () => {
    const purpose = 'p'.repeat(PILL_ACTIVITY_MAX_CHARS + 5)
    const spy = vi.fn(labelOf)
    const out = resolvePillActivity({ ...base, streamState: 'tool_running', detail: tool(purpose), running: true, labelOf: spy })
    expect(out.kind).toBe('tool')
    expect(out.text).toBe('p'.repeat(PILL_ACTIVITY_MAX_CHARS - 1) + '…')
    // The DETAIL itself is handed to the seam — purpose, tool name, derived
    // title and all — so the preference logic lives there, not here.
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ kind: 'tool', purpose, toolName: 'shell' }))
  })

  it('a tool status the seam has no label for reads as thinking', () => {
    expect(resolvePillActivity({ ...base, streamState: 'tool_running', detail: tool('', ''), running: true }))
      .toEqual({ kind: 'thinking', text: 'Thinking…' })
  })

  it('a returned tool call reads as thinking — the model is reading the result', () => {
    expect(resolvePillActivity({ ...base, streamState: 'tool_running', detail: tool('old purpose'), toolReturned: true, running: true }))
      .toEqual({ kind: 'thinking', text: 'Thinking…' })
  })

  it('a thinking status carries the seam copy, a server label passes through, streaming is the pill\'s own writing word', () => {
    expect(resolvePillActivity({ ...base, streamState: 'streaming', detail: { kind: 'thinking', ts: 1 }, running: true }))
      .toEqual({ kind: 'thinking', text: 'Thinking…' })
    // No text: the page owns the word (`pill_writing`), never the seam's
    // "Streaming".
    expect(resolvePillActivity({ ...base, streamState: 'streaming', detail: { kind: 'streaming', ts: 1 }, running: true }))
      .toEqual({ kind: 'writing' })
    expect(resolvePillActivity({ ...base, running: true, detail: { kind: 'thinking', label: 'Reading the repo', ts: 1 } }))
      .toEqual({ kind: 'thinking', text: 'Reading the repo' })
  })

  it('compacting and stopping name themselves, whatever the status says', () => {
    expect(resolvePillActivity({ ...base, streamState: 'compacting', detail: tool('x'), running: true })).toEqual({ kind: 'compacting' })
    expect(resolvePillActivity({ ...base, streamState: 'stopping', detail: tool('x'), running: true })).toEqual({ kind: 'stopping' })
  })

  it('busy with no status yet is working, or delegated when only sub-agents run', () => {
    expect(resolvePillActivity({ ...base, running: true })).toEqual({ kind: 'working' })
    expect(resolvePillActivity({ ...base, running: true, detail: { kind: 'idle', ts: 1 } })).toEqual({ kind: 'working' })
    expect(resolvePillActivity({ ...base, running: true, delegatedOnly: true })).toEqual({ kind: 'delegated' })
  })

  it('a run state that has not settled counts as busy even before the slots frame says so', () => {
    expect(resolvePillActivity({ ...base, streamState: 'streaming', detail: { kind: 'streaming', ts: 1 } }))
      .toEqual({ kind: 'writing' })
  })

  it('nothing running is idle, and a stale status from the last turn is never painted', () => {
    expect(resolvePillActivity({ ...base, detail: tool('old call') })).toEqual({ kind: 'idle' })
    expect(resolvePillActivity({ ...base, detail: { kind: 'thinking', ts: 1 } })).toEqual({ kind: 'idle' })
  })
})
