import { describe, it, expect, beforeEach } from 'vitest'
import { fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine, { resetOpenedDiffCards } from '../pages/chat/ToolCallLine'
import { presentToolDiff, isDiffToolMessage } from '../pages/chat/toolDiff'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

type ChatState = RootState['chat']

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

beforeEach(() => { localStorage.clear(); resetOpenedDiffCards() })

const UNIFIED_DIFF = [
  '--- /home/u/proj/src/app.py',
  '+++ /home/u/proj/src/app.py',
  '@@ -1,3 +1,3 @@',
  ' import os',
  '-x = 1',
  '+x = 2',
].join('\n')

function bigDiff(lines: number): string {
  const body = Array.from({ length: lines }, (_, i) => `+line ${i}`).join('\n')
  return `--- /dev/null\n+++ /home/u/proj/big.txt\n@@ -0,0 +1,${lines} @@\n${body}`
}

describe('presentToolDiff', () => {
  it('promotes an edit-kind unified diff to a full card', () => {
    expect(presentToolDiff('edit', UNIFIED_DIFF)).toEqual({ mode: 'card', code: UNIFIED_DIFF })
  })

  it('never promotes a non-edit kind, even when the input looks like a diff', () => {
    // A shell command's input can contain diff-shaped text (git apply, a
    // heredoc patch) — the kind gate is what keeps it un-promoted.
    expect(presentToolDiff('execute', UNIFIED_DIFF)).toBeNull()
    expect(presentToolDiff('read', UNIFIED_DIFF)).toBeNull()
    expect(presentToolDiff(undefined, UNIFIED_DIFF)).toBeNull()
    expect(presentToolDiff('', UNIFIED_DIFF)).toBeNull()
  })

  it('rejects edit-kind input that is not a unified diff', () => {
    expect(presentToolDiff('edit', '{"path": "/a/b", "command": "create"}')).toBeNull()
    expect(presentToolDiff('edit', '')).toBeNull()
    expect(presentToolDiff('edit', undefined)).toBeNull()
  })

  it('a transport-truncated diff is always a summary, flagged truncated', () => {
    // A truncated payload can sit under the card line cap (64 KiB of long
    // lines) while missing most of the change — it must never render as a
    // complete-looking card, and the chip shows a visible truncation note.
    const cut = `--- /a/big.txt\n+++ /a/big.txt\n@@ -1,9 +1,9 @@\n-old\n+new\n\\ diff truncated`
    const view = presentToolDiff('edit', cut)
    expect(view?.mode).toBe('summary')
    if (view?.mode === 'summary') expect(view.truncated).toBe(true)
    // An intact diff is never flagged.
    const intact = presentToolDiff('edit', UNIFIED_DIFF)
    expect(intact?.mode).toBe('card')
  })

  it('degrades an over-cap diff to a summary — never to nothing', () => {
    // Under the relaxed prompt the model no longer restates tool edits, so a
    // dropped card would leave a large edit with zero transcript trace.
    const view = presentToolDiff('edit', bigDiff(410))
    expect(view?.mode).toBe('summary')
    if (view?.mode === 'summary') {
      expect(view.path).toBe('/home/u/proj/big.txt')
      expect(view.added).toBe(410)
      expect(view.removed).toBe(0)
    }
    // At or under the cap it stays a full card.
    expect(presentToolDiff('edit', bigDiff(5))?.mode).toBe('card')
  })
})

describe('isDiffToolMessage', () => {
  const base: ChatMessage = {
    role: 'tool',
    content: '🔧 fs_write',
    cls: '',
    meta: { tool_call_id: 'tc_1', kind: 'edit', input: UNIFIED_DIFF },
  }

  it('matches a persisted edit-tool message carrying a diff (card or summary)', () => {
    expect(isDiffToolMessage(base)).toBe(true)
    expect(isDiffToolMessage({ ...base, meta: { ...base.meta, input: bigDiff(401) } })).toBe(true)
  })

  it('rejects non-tool roles, hidden tool rows, and rows without meta.kind', () => {
    expect(isDiffToolMessage({ ...base, role: 'assistant' })).toBe(false)
    expect(isDiffToolMessage({ ...base, content: '🚫 fs_write' })).toBe(false)
    // Rows persisted before meta.kind existed never promote — fail-safe.
    expect(isDiffToolMessage({ ...base, meta: { tool_call_id: 'tc_1', input: UNIFIED_DIFF } })).toBe(false)
  })
})

describe('ToolCallLine diff presentation', () => {
  function editMsg(): ChatMessage {
    return { role: 'tool', content: '🔧 fs_write', cls: '', meta: { tool_call_id: 'tc_d1', purpose: 'Edit app.py' } }
  }

  it('starts an edit tool\'s unified diff folded to its chip, and opens on click', () => {
    const store = createTestStore({
      chat: {
        messages: [editMsg()],
        toolLog: [{ type: 'tool', text: 'fs_write', kind: 'edit', input: UNIFIED_DIFF, tool_call_id: 'tc_d1', output: 'ok', ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    const { container, getByText } = renderWithProviders(<ToolCallLine message={editMsg()} running={false} />, { store })
    // Folded by default: a multi-edit turn would otherwise stack a full patch
    // per file and push the answer off screen.
    expect(container.querySelector('.diff-block')).toBeNull()
    // Asserted by testid, not just by text: the card-opening chip and the
    // details-panel chip look alike, and only the testid tells them apart —
    // which is what the fold-by-default screenshot harness grabs.
    expect(container.querySelector('[data-testid="tool-diff-chip"]')).toBeTruthy()
    fireEvent.click(getByText('app.py'))
    expect(container.querySelector('.diff-block')).toBeTruthy()
  })

  it('renders a summary chip (not a card) for an over-cap edit diff', () => {
    const store = createTestStore({
      chat: {
        messages: [editMsg()],
        toolLog: [{ type: 'tool', text: 'fs_write', kind: 'edit', input: bigDiff(410), tool_call_id: 'tc_d1', output: 'ok', ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    const { container, getByText } = renderWithProviders(<ToolCallLine message={editMsg()} running={false} />, { store })
    expect(container.querySelector('.diff-block')).toBeNull()
    expect(getByText('big.txt')).toBeTruthy()
    expect(getByText('+410')).toBeTruthy()
    // The summary chip expands the details panel; there is no card for it to
    // open, so it must NOT claim the card-opening testid.
    expect(container.querySelector('[data-testid="tool-diff-summary-chip"]')).toBeTruthy()
    expect(container.querySelector('[data-testid="tool-diff-chip"]')).toBeNull()
  })

  it('does not render a card for a shell tool with diff-shaped input', () => {
    const store = createTestStore({
      chat: {
        messages: [editMsg()],
        toolLog: [{ type: 'tool', text: 'shell', kind: 'execute', input: UNIFIED_DIFF, tool_call_id: 'tc_d1', output: 'ok', ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    const { container } = renderWithProviders(<ToolCallLine message={editMsg()} running={false} />, { store })
    expect(container.querySelector('.diff-block')).toBeNull()
  })

  it('renders the card from persisted meta on a historical row (no toolLog entry)', () => {
    const msg: ChatMessage = {
      role: 'tool',
      content: '🔧 fs_write',
      cls: '',
      meta: { tool_call_id: 'tc_hist', kind: 'edit', input: UNIFIED_DIFF },
    }
    const store = createTestStore({
      chat: { messages: [msg], toolLog: [], slotRunning: false } as unknown as ChatState,
    })
    const { container, getByText } = renderWithProviders(<ToolCallLine message={msg} running={false} />, { store })
    // The promotion is what this asserts, so open the fold and check the patch
    // came from meta.input rather than a live toolLog entry.
    fireEvent.click(getByText('app.py'))
    expect(container.querySelector('.diff-block')).toBeTruthy()
  })

  it('suppresses the card for a rejected edit — the change was not applied', () => {
    // A first-class diff card dominates the pill's small red status icon; a
    // reader scanning history would believe the file changed. The diff stays
    // readable in the expanded details panel.
    const store = createTestStore({
      chat: {
        messages: [editMsg()],
        toolLog: [{ type: 'tool', text: 'fs_write', kind: 'edit', input: UNIFIED_DIFF, tool_call_id: 'tc_d1', rejected: true, ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    const { container } = renderWithProviders(<ToolCallLine message={editMsg()} running={false} />, { store })
    expect(container.querySelector('.diff-block')).toBeNull()
  })

  it('the chip opens the card and its header control folds it back', async () => {
    const store = createTestStore({
      chat: {
        messages: [editMsg()],
        toolLog: [{ type: 'tool', text: 'fs_write', kind: 'edit', input: UNIFIED_DIFF, tool_call_id: 'tc_d1', output: 'ok', ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    const { container, getByText, findByLabelText, queryByText } = renderWithProviders(<ToolCallLine message={editMsg()} running={false} />, { store })
    // Folded: the chip is the open handle.
    expect(container.querySelector('.diff-block')).toBeNull()
    fireEvent.click(getByText('app.py'))
    // An OPEN card shows no chip — its own header carries the facts.
    expect(container.querySelector('.diff-block')).toBeTruthy()
    expect(queryByText('app.py')).toBeNull()
    // Pierre's header (and the fold control slotted into it) mounts async.
    fireEvent.click(await findByLabelText('Hide diff'))
    expect(container.querySelector('.diff-block')).toBeNull()
    expect(getByText('app.py')).toBeTruthy()
  })

  it('an expansion survives unmount/remount (virtualized transcript)', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ collapseAllSteps: false }))
    const mkStore = () => createTestStore({
      chat: {
        messages: [editMsg()],
        toolLog: [{ type: 'tool', text: 'fs_write', kind: 'edit', input: UNIFIED_DIFF, tool_call_id: 'tc_persist', output: 'ok', ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    const msg: ChatMessage = { role: 'tool', content: '🔧 fs_write', cls: '', meta: { tool_call_id: 'tc_persist', purpose: 'Edit app.py' } }
    const first = renderWithProviders(<ToolCallLine message={msg} running={false} />, { store: mkStore() })
    fireEvent.click(first.getByText('app.py'))
    expect(first.container.querySelector('.diff-block')).toBeTruthy()
    first.unmount()
    // Remount (what virtualizer recycling does): the expansion is remembered,
    // so a card being read does not snap shut on scroll.
    const second = renderWithProviders(<ToolCallLine message={msg} running={false} />, { store: mkStore() })
    expect(second.container.querySelector('.diff-block')).toBeTruthy()
    expect(second.queryByText('app.py')).toBeNull()
  })
})
