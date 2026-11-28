/**
 * An edit row whose change the turn's changed-files card records (the gateway
 * stamps `meta.file_change_recorded`) folds like any other tool row, in the
 * running window and in both settled modes. An unstamped edit row keeps the
 * diff-card rule and stays out of both folds (rfc-fold-recorded-edit-cards.md).
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import TurnBlock from '../pages/chat/TurnBlock'
import type { DisplayItem, TurnItem } from '../pages/chat/types'

const DIFF = '--- /a/b.py\n+++ /a/b.py\n@@ -1,2 +1,2 @@\n import os\n-x = 1\n+x = 2'

const edit = (id: string, idx: number, recorded: boolean): TurnItem => ({
  kind: 'single',
  msg: {
    role: 'tool',
    content: `🔧 str_replace ${id}`,
    ts: `e${idx}`,
    meta: { tool_call_id: id, kind: 'edit', input: DIFF, ...(recorded ? { file_change_recorded: true } : {}) },
  },
  idx,
})
const read = (n: number): TurnItem => ({
  kind: 'single',
  msg: { role: 'tool', content: `🔧 Running: read${n}`, ts: `r${n}`, meta: { tool_call_id: `read-${n}` } },
  idx: 100 + n,
})
const answer: TurnItem = {
  kind: 'single',
  msg: { role: 'assistant', content: 'Edited the file with plenty of descriptive text to be substantive here.', ts: 'z' },
  idx: 999,
}
const turnOf = (items: TurnItem[], complete: boolean): Extract<DisplayItem, { kind: 'turn' }> => ({ kind: 'turn', items, complete })

const renderTurn = (items: TurnItem[], complete: boolean, collapseAll = false) =>
  render(
    <TurnBlock
      turn={turnOf(items, complete)}
      collapseAll={collapseAll}
      renderItem={(it) => (
        <div data-testid={`item-${it.kind === 'single' ? ((it.msg.meta?.tool_call_id as string) ?? it.msg.role) : 'group'}`} />
      )}
    />,
  )

/** The fold row holding the item with this id, if it is rendered. */
const rowOf = (id: string) =>
  Array.from(document.querySelectorAll<HTMLElement>('[data-fold-row]'))
    .find(r => r.querySelector(`[data-testid="item-${id}"]`))
/** A running turn keeps every row mounted, so a missing row is a test failure, not a fold. */
const isFolded = (id: string) => {
  const row = rowOf(id)
  expect(row, `row ${id} is rendered`).toBeDefined()
  return row!.getAttribute('data-collapsed') === 'true'
}

describe('TurnBlock — recorded edit rows fold', () => {
  it('settled default mode: a recorded edit folds, an unrecorded one stays open', () => {
    renderTurn([read(0), edit('tc-rec', 1, true), edit('tc-old', 2, false), answer], true)
    expect(screen.queryByTestId('item-tc-rec')).not.toBeInTheDocument()
    expect(screen.getByTestId('item-tc-old')).toBeInTheDocument()
  })

  it('settled collapseAll mode: a recorded edit counts as a folded step', () => {
    renderTurn([read(0), edit('tc-rec', 1, true), edit('tc-old', 2, false), answer], true, true)
    expect(screen.getByText('Worked through 2 steps')).toBeInTheDocument()
    expect(screen.queryByTestId('item-tc-rec')).not.toBeInTheDocument()
    expect(screen.getByTestId('item-tc-old')).toBeInTheDocument()
  })

  it('running window: a recorded edit older than the newest five folds', () => {
    renderTurn([edit('tc-rec', 0, true), ...[1, 2, 3, 4, 5].map(read)], false)
    expect(isFolded('tc-rec')).toBe(true)
    expect([1, 2, 3, 4, 5].some(n => isFolded(`read-${n}`))).toBe(false)
  })

  it('running window: an unrecorded edit never folds and is not counted', () => {
    renderTurn([edit('tc-old', 0, false), ...[1, 2, 3, 4, 5].map(read)], false)
    expect(isFolded('tc-old')).toBe(false)
    expect(screen.queryAllByRole('button').some(b => b.hasAttribute('aria-expanded'))).toBe(false)
  })

  it('a stamp that is not exactly true keeps the row open', () => {
    const odd: TurnItem = { ...edit('tc-odd', 1, false) }
    if (odd.kind === 'single') odd.msg.meta = { ...odd.msg.meta, file_change_recorded: 'true' }
    renderTurn([read(0), odd, answer], true)
    expect(screen.getByTestId('item-tc-odd')).toBeInTheDocument()
  })
})
