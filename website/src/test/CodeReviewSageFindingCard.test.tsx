// The finding card's readability contract, plus the design-facet split.
//
// What these lock down is layout SEMANTICS, not pixels: that the headline leads,
// that a record without one still renders, that a missing field collapses its row
// instead of leaving a labelled blank, that choosing a finding and drafting it sit
// in the same control group, and that multi-facet design prose is split into lines
// rather than handed to the markdown renderer as one blob.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { Finding, ReportRow, RunReport } from '../apps/code-review-sage/lib/types'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span data-testid="md">{content}</span>,
}))

const FindingCard = (await import('../apps/code-review-sage/components/FindingCard')).default
const ReportView = (await import('../apps/code-review-sage/components/ReportView')).default
const ShipSummaryCard = (await import('../apps/code-review-sage/components/ShipSummaryCard')).default

function finding(over: Partial<Finding> = {}): Finding {
  return {
    dimension: 'Correctness',
    severity: 'red',
    file: 'src/a.ts',
    line: 88,
    headline: 'Revoking the blob URL blanks every image already in scrollback.',
    observation: 'cleanup() revokes a shared blob URL.',
    consequence: 'Historic messages lose their images.',
    suggestion: 'Revoke on unmount instead.',
    snippet: 'URL.revokeObjectURL(src)',
    ...over,
  }
}

describe('FindingCard', () => {
  it('leads with the headline and the severity word', () => {
    render(<FindingCard finding={finding()} />)
    expect(screen.getByText('Revoking the blob URL blanks every image already in scrollback.'))
      .toBeTruthy()
    // "must-fix" replaces the old icon + coloured dimension pairing, so severity
    // is stated once in words rather than twice in decoration.
    expect(screen.getByText(/must-fix · Correctness/)).toBeTruthy()
    expect(screen.getByText('src/a.ts:88')).toBeTruthy()
  })

  it('says should-fix for a yellow finding', () => {
    render(<FindingCard finding={finding({ severity: 'yellow' })} />)
    expect(screen.getByText(/should-fix/)).toBeTruthy()
  })

  it('renders a record with no headline without an empty heading', () => {
    // Reviews recorded before the field exist on disk and are still rendered.
    const { container } = render(<FindingCard finding={finding({ headline: undefined })} />)
    expect(screen.getByText('cleanup() revokes a shared blob URL.')).toBeTruthy()
    expect(container.querySelector('.text-\\[13\\.5px\\]')).toBeNull()
  })

  it('labels each prose row so the sections are separated by structure', () => {
    render(<FindingCard finding={finding()} />)
    for (const label of ['Observation', 'Consequence', 'Suggestion']) {
      expect(screen.getByText(label)).toBeTruthy()
    }
  })

  it('collapses a row whose field is empty instead of showing a bare label', () => {
    render(<FindingCard finding={finding({ consequence: '', suggestion: '   ' })} />)
    expect(screen.getByText('Observation')).toBeTruthy()
    expect(screen.queryByText('Consequence')).toBeNull()
    expect(screen.queryByText('Suggestion')).toBeNull()
  })

  it('renders the snippet verbatim in a <pre>, never through markdown', () => {
    render(<FindingCard finding={finding()} />)
    const snippet = screen.getByText('URL.revokeObjectURL(src)')
    expect(snippet.tagName).toBe('PRE')
    expect(snippet.closest('[data-testid="md"]')).toBeNull()
    // Prose, by contrast, must go through it.
    expect(screen.getByText('cleanup() revokes a shared blob URL.')
      .closest('[data-testid="md"]')).not.toBeNull()
  })

  it('puts the select checkbox in the same control group as the draft button', () => {
    // They used to sit at opposite ends of the card with the whole body between
    // them, although ticking a finding and drafting it are one decision.
    render(
      <FindingCard finding={finding()} selectable selected={false}
        onToggle={() => {}} onPost={() => {}} />,
    )
    const checkbox = screen.getByRole('checkbox')
    const button = screen.getByRole('button')
    const group = checkbox.closest('div')?.parentElement
    expect(group).not.toBeNull()
    expect(group!.contains(button)).toBe(true)
  })

  it('shows no action row when the finding can neither be selected nor drafted', () => {
    render(<FindingCard finding={finding()} />)
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.queryByRole('checkbox')).toBeNull()
  })
})

/** Render a report and open its single row — the design detail lives in the
 * row's expanded body, which is collapsed on first paint. */
function renderRow(row: Partial<ReportRow>) {
  render(<ReportView report={reportWith(row)} />)
  fireEvent.click(screen.getByText('PR title').closest('button') as HTMLButtonElement)
}

function reportWith(row: Partial<ReportRow>): RunReport {
  return {
    run_id: 'run-1', status: 'done', ready: true,
    bands: { red: 1, yellow: 0, green: 0 },
    generated_at: '2026-08-08T00:00:00Z', total: 1, report_slug: null,
    rows: [{
      change_id: 'c1', url: 'https://github.com/o/r/pull/11', title: 'PR title',
      band: 'red', why: '1× red', score: 80, design_risk: 'high', blast: 'LARGE',
      red: 1, yellow: 0, deep_reviewed: true, gate_verdict: 'BLOCK',
      ...row,
    } as ReportRow],
  }
}

describe('design facets', () => {
  const multi = [
    'Root cause: targets the URL cache, not the React key',
    'Architectural fit: reuses the existing Ctx pattern',
    'Proportionality: frontend only, alternatives measured',
  ].join('\n')

  it('splits a multi-line solution assessment into one node per facet', () => {
    // The reviewer is asked for short labelled facets on SEPARATE LINES. This view
    // used to pass the whole value to the markdown renderer, which ran them
    // together into one dense paragraph while the archived HTML report split them.
    renderRow({ solution_assessment: multi })
    for (const label of ['Root cause', 'Architectural fit', 'Proportionality']) {
      expect(screen.getByText(label)).toBeTruthy()
    }
    // The label is pulled out of the line, so the value no longer carries it.
    expect(screen.getByText('targets the URL cache, not the React key')).toBeTruthy()
  })

  it('keeps a single-line value on the markdown renderer', () => {
    renderRow({ solution_assessment: 'One short line.' })
    expect(screen.getByText('One short line.').closest('[data-testid="md"]')).not.toBeNull()
  })

  it('splits one long unlabelled blob into sentences rather than a wall', () => {
    // Records predating the structured prompt are a single prose paragraph.
    const blob = `${'This is a sentence about the design. '.repeat(6)}And a final one.`
    renderRow({ solution_assessment: blob })
    expect(screen.getByText('And a final one.')).toBeTruthy()
  })

  it('does not treat an early colon in sentence-fallback prose as a label', () => {
    // A label column is only right for real facet lines. Prose like "The API
    // returns 404: …" is a sentence, and bolding the clause before the colon into
    // the narrow 96px column misreads it and crams the text.
    const blob = 'The API returns 404: the record was already swept. '.repeat(4)
    renderRow({ solution_assessment: blob })
    // Each sentence stays whole in one node; the clause before the colon is never
    // hoisted into the narrow label column.
    const sentences = screen.getAllByText(
      /^The API returns 404: the record was already swept\.$/)
    expect(sentences.length).toBeGreaterThan(1)
    expect(screen.queryByText('The API returns 404')).toBeNull()
  })
})

describe('ShipSummaryCard control grouping', () => {
  it('puts its checkbox in the same footer group as its draft button', () => {
    // It renders directly above the findings in one stacked list and carries the
    // same decision, so having the checkbox in the header while FindingCard moved
    // its own to the footer made a user hunt for one control in two corners.
    render(
      <ShipSummaryCard body="Not ready: one must-fix." selectable selected={false}
        onToggle={() => {}} onPost={() => {}} />,
    )
    const checkbox = screen.getByRole('checkbox')
    const button = screen.getByRole('button')
    const group = checkbox.closest('div')?.parentElement
    expect(group).not.toBeNull()
    expect(group!.contains(button)).toBe(true)
  })

  it('shares the finding card select label so the two read identically', () => {
    render(
      <ShipSummaryCard body="Not ready: one must-fix." selectable selected={false}
        onToggle={() => {}} onPost={() => {}} />,
    )
    expect(screen.getByText('Add to draft')).toBeTruthy()
  })
})
