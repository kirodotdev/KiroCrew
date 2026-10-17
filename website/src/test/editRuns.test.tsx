import { describe, it, expect, beforeEach } from 'vitest'
import { fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine, { resetOpenedDiffCards } from '../pages/chat/ToolCallLine'
import { selectEditRuns } from '../pages/chat/editRuns'
import { decodePathForDisplay } from '../utils/pathBasename'
import { deriveToolCallTitle } from '../utils/toolCallTitle'
import type { RootState } from '../store'
import type { ChatMessage, ToolActivity } from '../types'

type ChatState = RootState['chat']

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

beforeEach(() => { localStorage.clear(); resetOpenedDiffCards() })

const NO_LOG: ToolActivity[] = []
const BLOG = 'drafts/The%20Power%20of%20Running%20Remote%20Blog.md'

function diff(path: string, removed: string[], added: string[]): string {
  return [`--- ${path}`, `+++ ${path}`, `@@ -1,${removed.length + 1} +1,${added.length + 1} @@`, ' keep',
    ...removed.map(l => `-${l}`), ...added.map(l => `+${l}`)].join('\n')
}

function edit(id: string, input: string): ChatMessage {
  return { role: 'tool', content: '🔧 fs_write', cls: '', meta: { tool_call_id: id, kind: 'edit', input } }
}
const done = (id: string): ChatMessage => ({ role: 'tool', content: '✅ fs_write', cls: '', meta: { tool_call_id: id } })
const read = (id: string): ChatMessage => ({ role: 'tool', content: '🔧 fs_read', cls: '', meta: { tool_call_id: id, kind: 'read', input: '{"path":"a.md"}' } })

describe('selectEditRuns', () => {
  it('folds consecutive edits to one file into the first row', () => {
    const a = edit('a', diff(BLOG, ['x', 'y', 'z', 'w'], []))
    const b = edit('b', diff(BLOG, ['p'], ['q']))
    const c = edit('c', diff(BLOG, ['r'], ['s']))
    const thinking: ChatMessage = { role: 'thinking', content: 'next tweak', cls: '' }
    const msgs = [read('r'), done('r'), a, done('a'), thinking, b, done('b'), c]
    const runs = selectEditRuns(msgs, NO_LOG)
    expect(runs.membersByLead.get(a)).toEqual([a, b, c])
    expect([...runs.followers]).toEqual([b, c])
  })

  it('breaks the run on a visible row or a different file', () => {
    const a = edit('a', diff(BLOG, ['x'], ['y']))
    const b = edit('b', diff(BLOG, ['x'], ['y']))
    const other = edit('o', diff('drafts/other.md', ['x'], ['y']))
    const say: ChatMessage = { role: 'assistant', content: 'Tightened the intro.', cls: '' }
    expect(selectEditRuns([a, read('r'), b], NO_LOG).followers.size).toBe(0)
    expect(selectEditRuns([a, say, b], NO_LOG).followers.size).toBe(0)
    expect(selectEditRuns([a, other, b], NO_LOG).followers.size).toBe(0)
  })

  it('keeps an edit with any approval record on its own row', () => {
    const a = edit('a', diff(BLOG, ['x'], ['y']))
    const b = edit('b', diff(BLOG, ['x'], ['y']))
    const pending: ChatMessage = { role: 'permission', content: 'approve?', cls: '', meta: { tool_call_id: 'b' } }
    const rejected: ChatMessage = { ...pending, meta: { tool_call_id: 'b', resolved: 'rejected' } }
    const approved: ChatMessage = { ...pending, meta: { tool_call_id: 'b', resolved: 'approved' } }
    const denied: ChatMessage = { role: 'tool', content: '🚫 fs_write — Blocked by security policy: x', cls: '', meta: { tool_call_id: 'b' } }
    expect(selectEditRuns([a, pending, b], NO_LOG).followers.size).toBe(0)
    expect(selectEditRuns([a, rejected, b], NO_LOG).followers.size).toBe(0)
    expect(selectEditRuns([a, b, denied], NO_LOG).followers.size).toBe(0)
    // Approved: the row the user read and approved must not vanish into a run.
    expect(selectEditRuns([a, approved, b], NO_LOG).followers.has(b)).toBe(false)
    // Nor may an approved edit lead a run that swallows the next one.
    const aPerm: ChatMessage = { ...approved, meta: { tool_call_id: 'a', resolved: 'approved' } }
    expect(selectEditRuns([aPerm, a, b], NO_LOG).followers.size).toBe(0)
  })

  it('does not merge a call whose logged output was clamped', () => {
    const a = edit('a', diff(BLOG, ['x'], ['y']))
    const b = edit('b', diff(BLOG, ['x'], ['y']))
    const clamped: ToolActivity[] = [
      { type: 'tool', text: 'fs_write', ts: 1, tool_call_id: 'a', output: 'ok' },
      { type: 'tool', text: 'fs_write', ts: 2, tool_call_id: 'b', output: 'long', output_cut: { at: 4, count: 900 } },
    ]
    expect(selectEditRuns([a, b], clamped).followers.size).toBe(0)
    const leadClamped: ToolActivity[] = [
      { type: 'tool', text: 'fs_write', ts: 1, tool_call_id: 'a', output: 'long', output_cut: { at: 4, count: 900 } },
    ]
    expect(selectEditRuns([a, b], leadClamped).followers.size).toBe(0)
  })

  it('keeps a call the tool log marks rejected on its own row', () => {
    // A Stop rejects the in-flight call in the tool log only
    // (clearPendingPermissions); no permission or 🚫 message records it.
    const a = edit('a', diff(BLOG, ['x'], ['y']))
    const b = edit('b', diff(BLOG, ['x'], ['y']))
    const log: ToolActivity[] = [
      { type: 'tool', text: 'fs_write', ts: 1, tool_call_id: 'a', output: 'ok' },
      { type: 'tool', text: 'fs_write', ts: 2, tool_call_id: 'b', rejected: true },
    ]
    expect(selectEditRuns([a, b], log).followers.size).toBe(0)
    expect(selectEditRuns([a, b], NO_LOG).followers.has(b)).toBe(true)
    // Only the newest entry for a call counts, as for the row's own status.
    const replayed: ToolActivity[] = [...log, { type: 'tool', text: 'fs_write', ts: 3, tool_call_id: 'b', output: 'ok' }]
    expect(selectEditRuns([a, b], replayed).followers.has(b)).toBe(true)
  })

  it('keeps a row with a tool-risk badge on its own row', () => {
    const a = edit('a', diff(BLOG, ['x'], ['y']))
    const b = edit('b', diff(BLOG, ['x'], ['y']))
    const risky: ChatMessage = { ...b, meta: { ...b.meta, decisions_tool_risk: { turn_id: 't1', tool: 'fs_write', tier: 'risky', p: 0.9 } } }
    expect(selectEditRuns([a, risky], NO_LOG).followers.size).toBe(0)
    expect(selectEditRuns([risky, a], NO_LOG).followers.size).toBe(0)
  })

  it('does not merge a diff too large or truncated for a card', () => {
    const a = edit('a', diff(BLOG, ['x'], ['y']))
    const cut = edit('b', `${diff(BLOG, ['x'], ['y'])}\n\\ diff truncated`)
    expect(selectEditRuns([a, cut], NO_LOG).followers.size).toBe(0)
  })

  it('a repeated row for the same call hides with the run but adds no diff', () => {
    const a = edit('a', diff(BLOG, ['x'], ['y']))
    const again = edit('a', diff(BLOG, ['x'], ['y']))
    const runs = selectEditRuns([a, again], NO_LOG)
    expect(runs.followers.has(again)).toBe(true)
    expect(runs.membersByLead.size).toBe(0)
  })
})

describe('decodePathForDisplay', () => {
  it('decodes percent-escapes per segment', () => {
    expect(decodePathForDisplay(BLOG)).toBe('drafts/The Power of Running Remote Blog.md')
    expect(decodePathForDisplay('caf%C3%A9/men%C3%BC.md')).toBe('café/menü.md')
  })

  it('leaves a segment encoded when decoding would mislead or fails', () => {
    expect(decodePathForDisplay('a%2Fb.md')).toBe('a%2Fb.md')
    expect(decodePathForDisplay('a%0Ab.md')).toBe('a%0Ab.md')
    expect(decodePathForDisplay('a%E2%80%AEb.md')).toBe('a%E2%80%AEb.md')
    expect(decodePathForDisplay('100%.md')).toBe('100%.md')
    expect(decodePathForDisplay('x/%E0%A4%A.md')).toBe('x/%E0%A4%A.md')
  })

  it('decodes the path in a derived edit title', () => {
    const t = deriveToolCallTitle({ kind: 'edit', rawInput: diff(BLOG, ['x'], ['y']), title: 'fs_write' })
    expect(t.title).toContain('The Power of Running Remote Blog.md')
    expect(t.title).not.toContain('%20')
  })
})

describe('ToolCallLine edit runs', () => {
  function setup() {
    const a = edit('a', diff(BLOG, ['x', 'y', 'z', 'w'], []))
    const b = edit('b', diff(BLOG, ['p'], ['q']))
    const c = edit('c', diff(BLOG, ['r'], ['s']))
    const messages = [a, done('a'), b, done('b'), c, done('c')]
    const store = createTestStore({
      chat: { messages, toolLog: [], slotRunning: false } as unknown as ChatState,
    })
    return { a, b, c, store }
  }

  it('the lead row carries the run: one chip, summed counts, every diff', () => {
    const { a, store } = setup()
    const { container, getByText } = renderWithProviders(<ToolCallLine message={a} running={false} />, { store })
    const chip = container.querySelector<HTMLElement>('[data-testid="tool-diff-chip"]')!
    expect(chip.textContent).toContain('3 edits')
    expect(getByText('-6')).toBeTruthy()
    expect(getByText('+2')).toBeTruthy()
    fireEvent.click(chip)
    expect(container.querySelectorAll('.diff-block')).toHaveLength(3)
    expect(container.textContent).not.toContain('%20')
  })

  it('labels each card of an opened run "Edit N of M"', () => {
    const { a, store } = setup()
    const { container } = renderWithProviders(<ToolCallLine message={a} running={false} />, { store })
    fireEvent.click(container.querySelector<HTMLElement>('[data-testid="tool-diff-chip"]')!)
    const labels = [...container.querySelectorAll('[data-testid="edit-run-card-label"]')].map(el => el.textContent)
    expect(labels).toEqual(['Edit 1 of 3', 'Edit 2 of 3', 'Edit 3 of 3'])
  })

  it('a single edit card carries no "Edit N of M" label', () => {
    const a = edit('a', diff(BLOG, ['x'], ['y']))
    const store = createTestStore({ chat: { messages: [a], toolLog: [], slotRunning: false } as unknown as ChatState })
    const { container } = renderWithProviders(<ToolCallLine message={a} running={false} />, { store })
    fireEvent.click(container.querySelector<HTMLElement>('[data-testid="tool-diff-chip"]')!)
    expect(container.querySelector('[data-testid="edit-run-card-label"]')).toBeNull()
  })

  it("the merged row's details list every member's output, labelled", () => {
    const a = edit('a', diff(BLOG, ['x'], ['y']))
    const b0 = edit('b', diff(BLOG, ['p'], ['q']))
    const b: ChatMessage = { ...b0, meta: { ...b0.meta, output: 'old_str not found' } }
    const c = edit('c', diff(BLOG, ['r'], ['s']))
    const store = createTestStore({
      chat: {
        messages: [a, b, c],
        toolLog: [
          // The lead and the last call are live (tool log); b is history (meta.output).
          { type: 'tool', text: 'fs_write', ts: 1, tool_call_id: 'a', kind: 'edit', input: a.meta!.input as string, output: 'ok' },
          { type: 'tool', text: 'fs_write', ts: 3, tool_call_id: 'c', kind: 'edit', input: c.meta!.input as string, output: 'wrote 1 line' },
        ],
        slotRunning: false,
      } as unknown as ChatState,
    })
    const { container, getByRole } = renderWithProviders(<ToolCallLine message={a} running={false} />, { store })
    fireEvent.click(getByRole('button', { name: /Show details/i }))
    const text = container.textContent ?? ''
    const at = (s: string) => text.indexOf(s)
    for (const s of ['Edit 1 of 3', 'ok', 'Edit 2 of 3', 'old_str not found', 'Edit 3 of 3', 'wrote 1 line']) expect(at(s)).toBeGreaterThan(-1)
    expect(at('Edit 1 of 3')).toBeLessThan(at('Edit 2 of 3'))
    expect(at('Edit 2 of 3')).toBeLessThan(at('old_str not found'))
    expect(at('old_str not found')).toBeLessThan(at('Edit 3 of 3'))
    expect(at('Edit 3 of 3')).toBeLessThan(at('wrote 1 line'))
  })

  it('an edit approved by the user keeps its own row after the run', () => {
    const a = edit('a', diff(BLOG, ['x'], ['y']))
    const b = edit('b', diff(BLOG, ['p'], ['q']))
    const c = edit('c', diff(BLOG, ['r'], ['s']))
    const perm: ChatMessage = { role: 'permission', content: 'approve?', cls: '', meta: { tool_call_id: 'c', resolved: 'approved' } }
    const store = createTestStore({
      chat: { messages: [a, done('a'), b, done('b'), perm, c, done('c')], toolLog: [], slotRunning: false } as unknown as ChatState,
    })
    const lead = renderWithProviders(<ToolCallLine message={a} running={false} />, { store }).container
    expect(lead.querySelector<HTMLElement>('[data-testid="tool-diff-chip"]')!.textContent).toContain('2 edits')
    const own = renderWithProviders(<ToolCallLine message={c} running={false} />, { store }).container
    expect(own.innerHTML).not.toBe('')
    const chip = own.querySelector<HTMLElement>('[data-testid="tool-diff-chip"]')
    expect(chip).not.toBeNull()
    expect(chip!.textContent).not.toContain('edits')
    // The tag says why this edit is a row of its own; the merged lead has none.
    expect(own.querySelector('[data-testid="tool-approved-tag"]')?.textContent).toBe('Approved')
    expect(lead.querySelector('[data-testid="tool-approved-tag"]')).toBeNull()
  })

  it('tags an edit approved through a standing grant, not a rejected or pending one', () => {
    const tagFor = (resolved: string | undefined) => {
      const c = edit('c', diff(BLOG, ['r'], ['s']))
      const meta: Record<string, unknown> = { tool_call_id: 'c' }
      if (resolved !== undefined) meta.resolved = resolved
      const perm: ChatMessage = { role: 'permission', content: 'approve?', cls: '', meta }
      const store = createTestStore({ chat: { messages: [perm, c], toolLog: [], slotRunning: false } as unknown as ChatState })
      return renderWithProviders(<ToolCallLine message={c} running={false} />, { store })
        .container.querySelector('[data-testid="tool-approved-tag"]')
    }
    expect(tagFor('trust')).not.toBeNull()
    expect(tagFor('rejected')).toBeNull()
    expect(tagFor('rejected_once')).toBeNull()
    expect(tagFor(undefined)).toBeNull()
  })

  it('an auto-approved edit (no approval record) carries no tag', () => {
    const c = edit('c', diff(BLOG, ['r'], ['s']))
    const store = createTestStore({ chat: { messages: [c, done('c')], toolLog: [], slotRunning: false } as unknown as ChatState })
    const { container } = renderWithProviders(<ToolCallLine message={c} running={false} />, { store })
    expect(container.querySelector('[data-testid="tool-diff-chip"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="tool-approved-tag"]')).toBeNull()
  })

  it('a follower row draws nothing', () => {
    const { b, c, store } = setup()
    expect(renderWithProviders(<ToolCallLine message={b} running={false} />, { store }).container.innerHTML).toBe('')
    expect(renderWithProviders(<ToolCallLine message={c} running={false} />, { store }).container.innerHTML).toBe('')
  })

  it('after a Stop rejects the second edit, both rows draw on their own', () => {
    const a = edit('a', diff(BLOG, ['x'], ['y']))
    const b = edit('b', diff(BLOG, ['p'], ['q']))
    const store = createTestStore({
      chat: {
        messages: [a, b],
        toolLog: [
          { type: 'tool', text: 'fs_write', ts: 1, tool_call_id: 'a', kind: 'edit', input: a.meta!.input as string, output: 'ok' },
          { type: 'tool', text: 'fs_write', ts: 2, tool_call_id: 'b', kind: 'edit', input: b.meta!.input as string, rejected: true },
        ],
        slotRunning: false,
      } as unknown as ChatState,
    })
    const lead = renderWithProviders(<ToolCallLine message={a} running={false} />, { store }).container
    const chip = lead.querySelector<HTMLElement>('[data-testid="tool-diff-chip"]')
    expect(chip).not.toBeNull()
    expect(chip!.textContent).not.toContain('edits')
    expect(renderWithProviders(<ToolCallLine message={b} running={false} />, { store }).container.innerHTML).not.toBe('')
  })

  it('shows the file name decoded, once', () => {
    const { a, store } = setup()
    const { container, getByTestId } = renderWithProviders(<ToolCallLine message={a} running={false} />, { store })
    const label = getByTestId('tool-pill-label').textContent ?? ''
    expect(label).toContain('The Power of Running Remote Blog.md')
    expect(container.textContent).not.toContain('%20')
    // The label names the file, so the chip's visible text does not repeat it;
    // the name stays its accessible name.
    const chip = container.querySelector<HTMLElement>('[data-testid="tool-diff-chip"]')!
    expect(chip.querySelector('.font-mono')).toBeNull()
    expect(chip.querySelector('.sr-only')?.textContent).toBe('The Power of Running Remote Blog.md')
  })
})
