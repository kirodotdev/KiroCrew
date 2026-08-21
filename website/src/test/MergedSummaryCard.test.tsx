import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

import MergedSummaryCard from '../pages/chat/MergedSummaryCard'
import type { ChatMessage } from '../types'

/**
 * Pins the visible merged block a merge-back appends to a parent (issue
 * #3816): the card must name the fork it came from, say whether the summary
 * covers the whole fork or only its post-fork work, render the summarizer's
 * markdown body, and show the gap note when the parent advanced.
 */
const msg = (meta: Record<string, unknown>, content = '**Investigate** — completed'): ChatMessage =>
  ({
    role: 'merged_summary',
    content,
    ts: 1_700_000_000,
    meta: { kind: 'merged_summary', ...meta },
  }) as unknown as ChatMessage

describe('MergedSummaryCard', () => {
  it('names the fork and renders the summary body with a localized gap note', () => {
    render(
      <MergedSummaryCard
        message={msg({
          merged_from: 'dashboard:fork-1',
          merged_from_title: 'Backoff deep-dive',
          advanced: 2,
          gap_note: 'Parent advanced 2 message(s) since this fork; merged at the current tail.',
        })}
      />,
    )
    const card = screen.getByTestId('merged-summary-card')
    expect(card).toHaveTextContent('Backoff deep-dive')
    // The markdown body renders through MarkdownRenderer (bold survives).
    expect(screen.getByText('Investigate')).toBeInTheDocument()
    // Structured meta wins: the note is built client-side from the count
    // (localized), not echoed from the persisted backend-English string.
    expect(card).toHaveTextContent(/parent advanced 2 messages/i)
    expect(card).not.toHaveTextContent(/message\(s\)/i)
  })

  it('ignores a stray persisted gap_note string — only meta.advanced renders a note', () => {
    // The persisted-English fallback was removed (First Principles review):
    // the backend writes ``advanced`` under exactly the condition a note is
    // warranted, so a block carrying only a raw string renders no note.
    render(
      <MergedSummaryCard
        message={msg({
          merged_from_title: 'Old block',
          gap_note: 'Parent advanced 3 message(s) since this fork; merged at the current tail.',
        })}
      />,
    )
    expect(screen.getByTestId('merged-summary-card')).not.toHaveTextContent(/message\(s\)/i)
  })

  it('shows the generic localized header when the fork was untitled, and omits the gap note', () => {
    render(<MergedSummaryCard message={msg({ merged_from: 'dashboard:fork-2' })} />)
    const card = screen.getByTestId('merged-summary-card')
    // An untitled fork carries an empty title (backend sends none), so the
    // card uses its own localized generic header rather than echoing a
    // session key or a persisted English "Untitled".
    expect(card).toHaveTextContent(/merged from a fork/i)
    expect(card).not.toHaveTextContent('dashboard:fork-2')
    expect(card).not.toHaveTextContent(/advanced/i)
  })

  it('labels a head-fork summary as covering the full fork', () => {
    render(
      <MergedSummaryCard
        message={msg({ merged_from_title: 'F', covers_full_fork: true })}
      />,
    )
    // The two subtitle variants must differ so the label carries information.
    const full = screen.getByTestId('merged-summary-card').textContent ?? ''
    render(<MergedSummaryCard message={msg({ merged_from_title: 'F' })} />)
    const cards = screen.getAllByTestId('merged-summary-card')
    const partial = cards[cards.length - 1].textContent ?? ''
    expect(full).not.toEqual(partial)
  })
})
