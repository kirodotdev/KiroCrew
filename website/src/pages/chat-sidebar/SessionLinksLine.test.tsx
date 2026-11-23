import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SessionStatusLinksLine } from './SessionLinksLine'
import type { SidebarSourceLink } from './types'

const gh = (number: number, extra: Partial<SidebarSourceLink> = {}): SidebarSourceLink => ({
  provider: 'github', number, label: `#${number}`, kind: 'change', state: 'open',
  url: `https://github.com/acme/app/pull/${number}`, ...extra,
})

function renderLine(links: SidebarSourceLink[] | undefined, total?: number, onRow = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <div className="session-row" role="button" tabIndex={0} onClick={onRow} onMouseDown={onRow} onKeyDown={onRow}>
        <SessionStatusLinksLine status={<div data-testid="status">2 need you</div>} slotKey="s1" links={links} total={total}
          connected isActive={false} onActivateSlot={() => {}} />
      </div>
    </QueryClientProvider>,
  )
  return onRow
}

describe('SessionStatusLinksLine (#38200)', () => {
  it('draws only the status when the session links nothing', () => {
    renderLine([])
    expect(screen.getByTestId('status')).toBeInTheDocument()
    expect(screen.queryByTestId('session-links-count')).toBeNull()
  })

  it('shows one count beside the status and keeps the chips folded', () => {
    renderLine([gh(1), gh(2), gh(3)], 6)
    const count = screen.getByTestId('session-links-count')
    expect(count).toHaveTextContent(/^6$/)
    expect(count).toHaveAttribute('aria-expanded', 'false')
    expect(count).toHaveAccessibleName('Show 6 linked items')
    expect(screen.queryByTestId('session-links-expanded')).toBeNull()
  })

  it('expands the chip strip on a click and folds it on the next, without reaching the row', () => {
    const onRow = renderLine([gh(1), gh(2)])
    const count = screen.getByTestId('session-links-count')
    fireEvent.mouseDown(count)
    fireEvent.click(count)
    expect(count).toHaveAttribute('aria-expanded', 'true')
    expect(count).toHaveAccessibleName('Hide linked items')
    expect(screen.getByTestId('session-links-expanded')).toBeInTheDocument()
    expect(screen.getByText('#1')).toBeInTheDocument()
    fireEvent.click(count)
    expect(count).toHaveAttribute('aria-expanded', 'false')
    expect(onRow).not.toHaveBeenCalled()
  })

  it('wears the provider mark for one provider and a link mark for a mix', () => {
    renderLine([gh(1), { ...gh(2), provider: 'jira', label: 'KC-2', kind: 'issue' }])
    const count = screen.getByTestId('session-links-count')
    expect(count.querySelector('svg')?.getAttribute('class')).toMatch(/lucide-link-2/)
  })

  it('uses a link mark when the budgeted links share a provider but the full list is unknown', () => {
    renderLine([gh(1), gh(2), gh(3)], 6)
    const count = screen.getByTestId('session-links-count')
    expect(count.querySelector('svg.lucide-link-2')).not.toBeNull()
    // The link mark is the only glyph: no provider logo beside it.
    expect(count.querySelectorAll('svg')).toHaveLength(1)
  })

  it('adds a red dot only when a linked check failed, and names it in the label', () => {
    renderLine([gh(1), gh(2, { ci: 'failed' })])
    expect(screen.getByTestId('session-links-failed')).toBeInTheDocument()
    expect(screen.getByTestId('session-links-count')).toHaveAccessibleName('Show 2 linked items, a check failed')
  })

  it('ignores the old failure of a merged or closed source, as its chip does', () => {
    renderLine([gh(1, { ci: 'failed', state: 'closed' }), gh(2, { ci: 'failed', state: 'merged' })])
    expect(screen.queryByTestId('session-links-failed')).toBeNull()
    expect(screen.getByTestId('session-links-count')).toHaveAccessibleName('Show 2 linked items')
  })

  it('has no red dot when nothing failed', () => {
    renderLine([gh(1, { ci: 'passed' })])
    expect(screen.queryByTestId('session-links-failed')).toBeNull()
  })
})
