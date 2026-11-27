import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mockApi = vi.hoisted(() => ({
  skillsBudget: vi.fn(),
  setSkillInjectOnTrigger: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import SkillContextBudget from '../pages/overview/SkillContextBudget'

const ROW = {
  key: 'web-verify',
  name: 'web-verify',
  size_bytes: 2137,
  deliveries: 3,
  chars: 6411,
  inject_on_trigger: true,
  always: false,
  owned: true,
  source: 'kirocrew',
  idle_days: 1.0,
}

function mount(extra: Record<string, unknown>) {
  mockApi.skillsBudget.mockResolvedValue({ window_days: 30, total_chars: 6411, rows: [ROW], ...extra })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SkillContextBudget onBack={vi.fn()} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
})

/** The startup `[Skills:]` index: how many skills the model sees by name. */
describe('SkillContextBudget — startup index count', () => {
  it('says how many skills the startup index lists of how many, and how to reach the rest', async () => {
    mount({ index_budget_chars: 24750, index_listed: 143, index_total: 212 })
    const line = await screen.findByTestId('startup-index-count')
    expect(line.textContent).toContain('143 of 212 skills listed by name at session start')
    // Which session the figure describes: a mapped agent or an open project sees its own index.
    expect(line.textContent).toContain('default agent with no project open')
    expect(line.textContent).toContain('skill_search')
  })

  it('states a complete index without the hidden-skills hint', async () => {
    mount({ index_budget_chars: 24750, index_listed: 12, index_total: 12 })
    const line = await screen.findByTestId('startup-index-count')
    expect(line.textContent).toContain('12 of 12 skills listed by name at session start')
    expect(line.textContent).not.toContain('skill_search')
  })

  it('uses the singular for a library of one skill', async () => {
    mount({ index_budget_chars: 24750, index_listed: 1, index_total: 1 })
    const line = await screen.findByTestId('startup-index-count')
    expect(line.textContent).toContain('1 of 1 skill listed by name')
    expect(line.textContent).not.toMatch(/\b1 skills\b/)
  })

  it('shows no count when the gateway does not report one (older backend, or a failed render)', async () => {
    mount({})
    await screen.findByText(/Web Verify/i)
    expect(screen.queryByTestId('startup-index-count')).toBeNull()
    mount({ index_budget_chars: 24750, index_listed: null, index_total: null })
    await screen.findAllByText(/Web Verify/i)
    expect(screen.queryByTestId('startup-index-count')).toBeNull()
  })
})
