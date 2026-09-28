/**
 * A skill with `always: true` reports chars === null: it is injected every turn,
 * but that injection is never recorded in the usage ledger, so the cost is not
 * measurable. Such a row must stay VISIBLE.
 *
 * The grouping filters key off the cost, and `null > 0` and `null === 0` are
 * BOTH false, so a null-cost row trivially falls through every group and
 * disappears from the table -- silent data loss on the one screen whose whole
 * job is to account for context spend.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mockApi = vi.hoisted(() => ({
  skillsBudget: vi.fn(),
  setSkillInjectOnTrigger: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import SkillContextBudget from '../pages/overview/SkillContextBudget'

const ALWAYS_SKILL = {
  key: 'pinned-skill',
  name: 'pinned-skill',
  size_bytes: 4096,
  deliveries: null,
  chars: null,
  inject_on_trigger: true,
  always: true,
  owned: true,
  source: 'kirocrew',
  idle_days: null,
}

const MEASURED_SKILL = {
  key: 'measured-skill',
  name: 'measured-skill',
  size_bytes: 1000,
  deliveries: 10,
  chars: 10000,
  inject_on_trigger: true,
  always: false,
  owned: true,
  source: 'kirocrew',
  idle_days: 1.0,
}

function renderBudget() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SkillContextBudget onBack={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('a skill whose cost is not measurable', () => {
  beforeEach(() => {
    mockApi.skillsBudget.mockReset()
    mockApi.setSkillInjectOnTrigger.mockReset()
  })

  it('is still listed instead of falling through every group', async () => {
    mockApi.skillsBudget.mockResolvedValue({
      window_days: 30,
      total_chars: 10000,
      rows: [ALWAYS_SKILL, MEASURED_SKILL],
    })
    renderBudget()
    await waitFor(() => expect(screen.getByText('Measured Skill')).toBeInTheDocument())
    // The row that reports no measurable cost must be on screen too.
    expect(screen.getByText('Pinned Skill')).toBeInTheDocument()
  })

  it('states it is injected every turn rather than printing a cost of zero', async () => {
    mockApi.skillsBudget.mockResolvedValue({
      window_days: 30,
      total_chars: 0,
      rows: [ALWAYS_SKILL],
    })
    renderBudget()
    await waitFor(() => expect(screen.getByText('Pinned Skill')).toBeInTheDocument())
    // Honest label, not a fabricated number and not an em-dash placeholder.
    expect(screen.getByText(/every turn/i)).toBeInTheDocument()
    expect(screen.queryByText(/^0 chars$/)).not.toBeInTheDocument()
  })

  it('does not drag the measured total down by counting null as zero', async () => {
    mockApi.skillsBudget.mockResolvedValue({
      window_days: 30,
      total_chars: 10000,
      rows: [ALWAYS_SKILL, MEASURED_SKILL],
    })
    renderBudget()
    await waitFor(() => expect(screen.getByText('Measured Skill')).toBeInTheDocument())
    // Both rows are in the counting group, so its header counts 2.
    expect(screen.getByText(/2 skills/)).toBeInTheDocument()
  })
})
