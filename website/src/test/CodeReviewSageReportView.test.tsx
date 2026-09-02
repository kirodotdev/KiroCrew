import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { RunReport } from '../apps/code-review-sage/lib/types'

// Render markdown-bearing prose as a tagged span so the test can (a) read the
// text and (b) prove a given string was routed THROUGH the markdown renderer —
// which lets the snippet test assert the code block was NOT.
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span data-testid="md">{content}</span>,
}))

const ReportView = (await import('../apps/code-review-sage/components/ReportView')).default

function makeReport(overrides: Partial<RunReport> = {}): RunReport {
  return {
    run_id: 'run-1',
    status: 'done',
    ready: true,
    bands: { red: 1, yellow: 1, green: 0 },
    generated_at: '2026-07-28T00:00:00Z',
    total: 2,
    report_slug: null,
    rows: [
      {
        change_id: 'c1',
        url: 'https://github.com/o/r/pull/11',
        title: 'Red PR title',
        band: 'red',
        why: 'blast=LARGE + 1× red',
        score: 80,
        design_risk: 'high',
        blast: 'LARGE',
        red: 1,
        yellow: 0,
        deep_reviewed: true,
        gate_verdict: 'BLOCK',
        design_headline: 'Headline text',
        problem: 'Problem text',
        findings: [
          {
            dimension: 'Security',
            severity: 'red',
            file: 'a.ts',
            line: 5,
            observation: 'Observation text',
            consequence: 'Consequence text',
            suggestion: 'Suggestion text',
            snippet: 'const secret = 42',
          },
        ],
      },
      {
        change_id: 'c2',
        url: 'https://github.com/o/r/pull/22',
        title: 'Yellow PR title',
        band: 'yellow',
        why: '2× yellow',
        score: 30,
        design_risk: 'medium',
        blast: 'MEDIUM',
        red: 0,
        yellow: 2,
        deep_reviewed: true,
        gate_verdict: 'CONCERNS',
        findings: [],
      },
    ],
    ...overrides,
  }
}

describe('ReportView', () => {
  it('renders a row per change with its PR link and title', () => {
    render(<ReportView report={makeReport()} />)
    expect(screen.getByText('Red PR title')).toBeInTheDocument()
    expect(screen.getByText('Yellow PR title')).toBeInTheDocument()
    // PR number derived from the URL, linking out.
    const link = screen.getByText('#11').closest('a') as HTMLAnchorElement
    expect(link).toBeTruthy()
    expect(link.getAttribute('href')).toBe('https://github.com/o/r/pull/11')
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('rel')).toBe('noreferrer')
  })

  it('band filter narrows the list to the selected band', () => {
    render(<ReportView report={makeReport()} />)
    // Both visible under "All".
    expect(screen.getByText('Red PR title')).toBeInTheDocument()
    expect(screen.getByText('Yellow PR title')).toBeInTheDocument()
    // Selecting the red band drops the yellow row.
    fireEvent.click(screen.getByRole('button', { name: /Needs review/ }))
    expect(screen.getByText('Red PR title')).toBeInTheDocument()
    expect(screen.queryByText('Yellow PR title')).not.toBeInTheDocument()
    // "All" resets.
    fireEvent.click(screen.getByRole('button', { name: /All/ }))
    expect(screen.getByText('Yellow PR title')).toBeInTheDocument()
  })

  it('marks the active filter chip with aria-pressed', () => {
    render(<ReportView report={makeReport()} />)
    const allChip = screen.getByRole('button', { name: /All/ })
    expect(allChip).toHaveAttribute('aria-pressed', 'true')
    const redChip = screen.getByRole('button', { name: /Needs review/ })
    expect(redChip).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(redChip)
    expect(redChip).toHaveAttribute('aria-pressed', 'true')
    expect(allChip).toHaveAttribute('aria-pressed', 'false')
  })

  it('expanding a row reveals its findings', () => {
    render(<ReportView report={makeReport()} />)
    // Finding detail is hidden until the row expands.
    expect(screen.queryByText('Observation text')).not.toBeInTheDocument()
    const expandBtn = screen.getByText('Red PR title').closest('button') as HTMLButtonElement
    expect(expandBtn).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(expandBtn)
    expect(expandBtn).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('Observation text')).toBeInTheDocument()
    expect(screen.getByText('Suggestion text')).toBeInTheDocument()
    // The design chain is shown too.
    expect(screen.getByText('Headline text')).toBeInTheDocument()
    expect(screen.getByText('Problem text')).toBeInTheDocument()
  })

  it('renders the code snippet in a <pre>, NOT through the markdown renderer', () => {
    render(<ReportView report={makeReport()} />)
    fireEvent.click(screen.getByText('Red PR title').closest('button') as HTMLButtonElement)
    const snippet = screen.getByText('const secret = 42')
    expect(snippet.tagName).toBe('PRE')
    // Prose fields went through the mocked markdown renderer (data-testid="md");
    // the snippet must not have.
    expect(snippet.closest('[data-testid="md"]')).toBeNull()
    expect(screen.getByText('Observation text').closest('[data-testid="md"]')).not.toBeNull()
  })

  it('marks only the comment being posted, not every unposted card', () => {
    // One shared flag made posting a single finding flip every other card to
    // "Drafting…", so the user believed the whole review was being published.
    // The row needs more than one postable card for that to be observable.
    const report = makeReport()
    report.rows[0] = {
      ...report.rows[0],
      ship_comment: 'Ship summary body',
      findings: [
        report.rows[0].findings![0],
        { ...report.rows[0].findings![0], file: 'b.ts', observation: 'Second finding' },
      ],
    }
    render(
      <ReportView
        report={report}
        onPostFinding={() => {}}
        isPosting={(key) => key === 'finding:0'}
      />,
    )
    fireEvent.click(screen.getByText('Red PR title').closest('button') as HTMLButtonElement)
    // Three postable cards (ship summary + two findings), one in flight.
    expect(screen.getAllByText('Drafting…')).toHaveLength(1)
  })

  it('shows the empty state when there are no rows', () => {
    render(<ReportView report={makeReport({ rows: [], bands: { red: 0, yellow: 0, green: 0 }, total: 0 })} />)
    expect(screen.getByText('Nothing flagged in this review.')).toBeInTheDocument()
  })

  it('calls onArchive when the Share button is clicked (no slug)', () => {
    const onArchive = vi.fn()
    render(<ReportView report={makeReport({ report_slug: null })} onArchive={onArchive} />)
    fireEvent.click(screen.getByRole('button', { name: /Share/ }))
    expect(onArchive).toHaveBeenCalledTimes(1)
  })

  it('links to the shared artifact copy when a slug exists (no Share button)', () => {
    render(<ReportView report={makeReport({ report_slug: 'focus-report-x' })} onArchive={vi.fn()} />)
    const link = screen.getByRole('link', { name: /Open shared copy/ })
    expect(link.getAttribute('href')).toBe('/artifacts/focus-report-x')
    expect(screen.queryByRole('button', { name: /Share/ })).not.toBeInTheDocument()
  })
})
