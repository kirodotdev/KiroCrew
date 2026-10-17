// The rail's "no specs match that filter" empty state used to be a dead end:
// the filter input has no clear control, so recovering meant re-finding the
// field and emptying it by hand. This pins the one-click exit — the empty
// state offers a Clear-filter action, and clicking it restores the full list
// with the input actually emptied (#7662).
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent, screen } from '@testing-library/react'
import SpecRail from '../apps/spec-builder/components/SpecRail'
import type { SpecSummary } from '../apps/spec-builder/api'

const spec = (name: string, over: Partial<SpecSummary> = {}): SpecSummary => ({
  name,
  phase: 'design',
  running: false,
  ...over,
} as SpecSummary)

function renderRail() {
  return render(
    <SpecRail
      specs={[spec('checkout-flow'), spec('dark-mode', { phase: 'tasks' })]}
      sel={null}
      setSel={() => {}}
      onNew={vi.fn()}
      width={260}
    />,
  )
}

describe('Spec Builder rail empty-filter state', () => {
  it('offers a Clear-filter action only when the filter matched nothing', () => {
    renderRail()

    // Unfiltered: every spec is grouped somewhere, so no empty state.
    expect(screen.getByText('checkout-flow')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /clear filter/i })).toBeNull()

    fireEvent.change(screen.getByRole('textbox', { name: /filter specs by name/i }), {
      target: { value: 'zebra' },
    })

    // The shared FilteredEmpty state appears, echoing the query that
    // excluded everything.
    expect(screen.getByTestId('filtered-empty')).toBeInTheDocument()
    expect(screen.getByText(/zebra/)).toBeInTheDocument()
    expect(screen.queryByText('checkout-flow')).toBeNull()
    expect(screen.getByRole('button', { name: /clear filter/i })).toBeInTheDocument()
  })

  it('clears the filter and restores the full list on click', () => {
    renderRail()

    const input = screen.getByRole('textbox', { name: /filter specs by name/i })
    fireEvent.change(input, { target: { value: 'zebra' } })
    expect(screen.getByTestId('filtered-empty')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /clear filter/i }))

    // The input is actually emptied — not just the list repainted — so the
    // user is back to the pre-filter state, ready to type a fresh needle.
    expect((input as HTMLInputElement).value).toBe('')
    expect(screen.getByText('checkout-flow')).toBeInTheDocument()
    expect(screen.getByText('dark-mode')).toBeInTheDocument()
    expect(screen.queryByTestId('filtered-empty')).toBeNull()
  })
})
