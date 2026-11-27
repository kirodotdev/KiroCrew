/**
 * The rows center their current occurrence once per find action (`reveal`),
 * through `useRevealMatch`: never for a row that is merely current (rows
 * arrived, its text changed, it mounted again as the reader scrolled past it),
 * never from the measuring farm's off-screen copy, and again for the next
 * action. An action stays pending until a row's poll is done moving the reader.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/react'
import type { ReactNode } from 'react'

const scroll = vi.hoisted(() => ({ calls: [] as Array<{ onSpent?: () => void }>, cancel: vi.fn() }))
vi.mock('../utils/searchScroll', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/searchScroll')>()),
  scrollCurrentMatchIntoView: (_root: Element | null | undefined, opts: { onSpent?: () => void } = {}) => {
    scroll.calls.push(opts)
    return scroll.cancel
  },
}))
// Each row's search effect paints through this; the spy counts which rows ran it.
const painted = vi.hoisted(() => ({ rows: [] as string[] }))
vi.mock('../utils/domHighlight', async (importOriginal) => {
  const real = await importOriginal<typeof import('../utils/domHighlight')>()
  return {
    ...real,
    applySearchHighlights: (...args: Parameters<typeof real.applySearchHighlights>) => {
      painted.rows.push(args[0].textContent ?? '')
      return real.applySearchHighlights(...args)
    },
  }
})
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}))
// Passthrough: its rAF loop is not what this file is about.
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))

import AssistantMessage from '../pages/chat/AssistantMessage'
import UserMessage from '../pages/chat/UserMessage'
import SearchHighlightContext, { MessageSearchScope, type RevealLedger } from '../hooks/SearchHighlightContext'
import { PierreFarmHoldContext } from '../components/pierreStaging'
import { getCurrentSearchRange } from '../utils/domHighlight'

function ledger(): RevealLedger {
  let used = 0
  return { pending: n => n > used, consume: n => { used = Math.max(used, n) } }
}

const rows: Array<[string, (content: string) => ReactNode]> = [
  ['AssistantMessage', content => <AssistantMessage content={content} isStreaming={false} slotRunning={false} />],
  ['UserMessage', content => <UserMessage content={content} messageTs="2026-11-20T10:00:00Z" renderContent={c => <p>{c}</p>} />],
]

describe.each(rows)('%s centers its current match once per find action', (_name, draw) => {
  beforeEach(() => {
    scroll.calls.length = 0
    scroll.cancel.mockClear()
    painted.rows.length = 0
  })

  function tree(content: string, reveal: number, revealLedger?: RevealLedger, farm = false) {
    const row = <MessageSearchScope messageIdx={0}>{draw(content)}</MessageSearchScope>
    return (
      <SearchHighlightContext.Provider value={{ term: 'alpha', caseSensitive: false, currentMessageIdx: 0, currentOccurrenceIdx: 0, reveal, revealLedger }}>
        {farm ? farmCopy(row) : row}
      </SearchHighlightContext.Provider>
    )
  }
  /** A row as the measuring farm draws its hidden copy (MeasureFarm). */
  const farmCopy = (row: ReactNode) => (
    <PierreFarmHoldContext.Provider value={true}><div data-farm-index="0">{row}</div></PierreFarmHoldContext.Provider>
  )

  it('a row that is merely current leaves the reader alone', () => {
    render(tree('alpha beta', 0, ledger()))
    expect(scroll.calls).toHaveLength(0)
  })

  it('centers for a pending action, again after a re-run that came first, and never once the poll moved the reader', () => {
    const l = ledger()
    const view = render(tree('alpha beta', 1, l))
    expect(scroll.calls).toHaveLength(1)
    // The text changes before the poll is done: that poll is cancelled, and
    // the action is still pending for the re-run.
    view.rerender(tree('alpha beta gamma', 1, l))
    expect(scroll.cancel).toHaveBeenCalledTimes(1)
    expect(scroll.calls).toHaveLength(2)
    scroll.calls[1].onSpent?.()
    view.rerender(tree('alpha beta gamma delta', 1, l))
    expect(scroll.calls).toHaveLength(2)
    // Scrolled away and back: the row mounts again for the same action.
    view.unmount()
    render(tree('alpha beta gamma delta', 1, l))
    expect(scroll.calls).toHaveLength(2)
  })

  // A far jump: the virtualizer swaps the row out and back in before its poll
  // is done. The action is still pending, so the row's next mount finishes it.
  it('centers again when the row mounts again before its poll was done', () => {
    const l = ledger()
    const view = render(tree('alpha beta', 1, l))
    expect(scroll.calls).toHaveLength(1)
    view.unmount()
    expect(scroll.cancel).toHaveBeenCalledTimes(1)
    render(tree('alpha beta', 1, l))
    expect(scroll.calls).toHaveLength(2)
  })

  it('centers again for the next find action', () => {
    const l = ledger()
    const view = render(tree('alpha beta', 1, l))
    scroll.calls[0].onSpent?.()
    view.rerender(tree('alpha beta', 2, l))
    expect(scroll.calls).toHaveLength(2)
  })

  it('the measuring farm\'s copy of the row never centers', () => {
    render(tree('alpha beta', 1, ledger(), true))
    expect(scroll.calls).toHaveLength(0)
  })

  // The farm draws hidden copies of unmeasured rows while the reader is idle,
  // and typing in the find bar is idle to it. A copy of the row a find action
  // just reached can be mounted beside that row. The page keeps one current
  // match range, and the row's centering reads it, so the copy must never take
  // it, and its removal must not take the row's away either.
  it('a farm copy of the current row never takes its current match, before or after it goes', () => {
    const l = ledger()
    const page = (copy: ReactNode) => (
      <SearchHighlightContext.Provider value={{ term: 'alpha', caseSensitive: false, currentMessageIdx: 0, currentOccurrenceIdx: 0, reveal: 1, revealLedger: l }}>
        {copy}
        <div data-testid="reader-row"><MessageSearchScope messageIdx={0}>{draw('alpha beta')}</MessageSearchScope></div>
      </SearchHighlightContext.Provider>
    )
    const copy = (content: string) => farmCopy(<MessageSearchScope messageIdx={0}>{draw(content)}</MessageSearchScope>)
    const view = render(page(copy('alpha beta')))
    const readerRow = view.getByTestId('reader-row')
    const onReaderRow = () => {
      const r = getCurrentSearchRange()
      return !!r && readerRow.contains(r.startContainer)
    }
    expect(onReaderRow()).toBe(true)
    // The copy's search effect runs again while it is mounted: here its text
    // changes; a code block that highlights itself does the same through the
    // row's MutationObserver.
    view.rerender(page(copy('alpha beta gamma')))
    expect(onReaderRow()).toBe(true)
    // The farm has measured the copy and drops it.
    view.rerender(page(null))
    expect(onReaderRow()).toBe(true)
  })

  it('nothing centers without a ledger', () => {
    render(tree('alpha beta', 1))
    expect(scroll.calls).toHaveLength(0)
  })

  it('a find action reaches only the row that holds the current match', () => {
    const l = ledger()
    const two = (reveal: number) => (
      <SearchHighlightContext.Provider value={{ term: 'alpha', caseSensitive: false, currentMessageIdx: 0, currentOccurrenceIdx: 0, reveal, revealLedger: l }}>
        <MessageSearchScope messageIdx={0}>{draw('alpha beta')}</MessageSearchScope>
        <MessageSearchScope messageIdx={1}>{draw('alpha gamma')}</MessageSearchScope>
      </SearchHighlightContext.Provider>
    )
    const runs = (text: string) => painted.rows.filter(t => t.includes(text)).length
    const view = render(two(1))
    expect(scroll.calls).toHaveLength(1)
    scroll.calls[0].onSpent?.()
    const otherRow = runs('alpha gamma')
    const currentRow = runs('alpha beta')
    view.rerender(two(2))
    expect(scroll.calls).toHaveLength(2)
    // The current row's search effect ran again for the new action; the other
    // row's did not run at all.
    expect(runs('alpha beta')).toBeGreaterThan(currentRow)
    expect(runs('alpha gamma')).toBe(otherRow)
  })
})
