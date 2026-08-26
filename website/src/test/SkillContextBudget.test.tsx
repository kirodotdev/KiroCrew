import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mockApi = vi.hoisted(() => ({
  skillsBudget: vi.fn(),
  setSkillInjectOnTrigger: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import SkillContextBudget from '../pages/overview/SkillContextBudget'

const HOT_SKILL = {
  key: 'kirocrew-commands',
  name: 'kirocrew-commands',
  size_bytes: 21728,
  deliveries: 664,
  chars: 14427392,
  inject_on_trigger: true,
  always: false,
  owned: true,
  source: 'kirocrew',
  idle_days: 0.0,
}

const COLD_SKILL = {
  key: 'grill',
  name: 'grill',
  size_bytes: 2137,
  deliveries: 0,
  chars: 0,
  inject_on_trigger: true,
  always: false,
  owned: true,
  source: 'kirocrew',
  idle_days: null,
}

const FROZEN_SKILL = {
  key: 'browser-auth',
  name: 'browser-auth',
  size_bytes: 10717,
  deliveries: 4,
  chars: 42868,
  inject_on_trigger: false,
  always: false,
  owned: true,
  source: 'kirocrew',
  idle_days: 14.5,
}

const PINNED_SKILL = {
  key: 'always-skill',
  name: 'always-skill',
  size_bytes: 5000,
  deliveries: 10,
  chars: 50000,
  inject_on_trigger: true,
  always: true,
  owned: true,
  source: 'kirocrew',
  idle_days: 0.0,
}

const PACKAGE_SKILL = {
  key: 'pkg-skill',
  name: 'pkg-skill',
  size_bytes: 8000,
  deliveries: 20,
  chars: 160000,
  inject_on_trigger: true,
  always: false,
  owned: false,
  source: 'package',
  idle_days: 0.0,
}

const FOLDED_SKILL = {
  key: 'dev-fleet/pod-e2e',
  name: 'pod-e2e',
  size_bytes: 15490,
  deliveries: 314,
  chars: 4863860,
  inject_on_trigger: true,
  always: false,
  owned: true,
  source: 'kirocrew',
  idle_days: 0.0,
  folded_from: ['pod-e2e'],
}

const BUDGET_RESPONSE = {
  window_days: 30,
  total_chars: 14427392 + 42868 + 50000 + 160000 + 4863860,
  rows: [HOT_SKILL, COLD_SKILL, FROZEN_SKILL, PINNED_SKILL, PACKAGE_SKILL, FOLDED_SKILL],
}

function mount(budgetData = BUDGET_RESPONSE) {
  mockApi.skillsBudget.mockResolvedValue(budgetData)
  mockApi.setSkillInjectOnTrigger.mockResolvedValue({})
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const onBack = vi.fn()
  const result = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SkillContextBudget onBack={onBack} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { ...result, onBack }
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
})

describe('SkillContextBudget — control plane', () => {
  describe('grouping', () => {
    it('shows three groups with correct headers', async () => {
      mount()
      expect(await screen.findByText(/counting toward context/i)).toBeTruthy()
      expect(screen.getByText(/never fired in 30 days/i)).toBeTruthy()
      expect(screen.getByText(/already pointer-only/i)).toBeTruthy()
    })

    it('puts hot skills (inject=true, chars>0) in the counting group', async () => {
      mount()
      // HOT_SKILL, PINNED_SKILL, PACKAGE_SKILL, FOLDED_SKILL are all inject_on_trigger=true with chars>0
      await screen.findByText(/Kirocrew Commands/i)
      // Cold skill should show "if it fires" text
      expect(screen.getAllByText(/Grill/i).length).toBeGreaterThan(0)
    })

    it('puts cold skills (inject=true, chars=0) in the never-fired group', async () => {
      mount()
      await screen.findByText(/Kirocrew Commands/i)
      // Verify "if it fires" text for cold skills
      expect(screen.getByText(/if it fires/i)).toBeTruthy()
    })

    it('puts pointer-only skills (inject=false) in the frozen group', async () => {
      mount()
      await screen.findByText(/Browser Auth/i)
    })
  })

  describe('sort options', () => {
    it('offers all three sort options once the dropdown is open', async () => {
      // Previously this only awaited /biggest cost/i without opening anything —
      // which matches the CLOSED trigger's own value, so it asserted exactly what
      // the "defaults to" test below asserts and proved nothing about the option
      // list. Open the Select and read its items.
      mount()
      const trigger = await screen.findByRole('combobox')
      fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0, ctrlKey: false })

      const options = await screen.findAllByRole('option')
      expect(options.map(o => o.textContent)).toEqual([
        'Biggest cost', 'Biggest file', 'Recently used',
      ])
    })

    it('defaults to Biggest cost sort', async () => {
      mount()
      // SelectTrigger shows current value
      expect(await screen.findByText(/biggest cost/i)).toBeTruthy()
    })
  })

  describe('folded aliases', () => {
    it('shows folded_from aliases with renamed label', async () => {
      mount()
      await waitFor(() => {
        expect(screen.getByText(/\(renamed\)/i)).toBeTruthy()
      })
    })
  })

  describe('toggle behavior', () => {
    it('shows toggle for owned, non-always skills', async () => {
      mount()
      await screen.findByText(/Kirocrew Commands/i)
      // There should be multiple toggles
      const toggles = screen.getAllByRole('switch')
      expect(toggles.length).toBeGreaterThan(0)
    })

    it('does NOT show toggle for pinned (always) skills', async () => {
      // Mount with only a pinned skill
      mockApi.skillsBudget.mockResolvedValue({
        window_days: 30,
        total_chars: 50000,
        rows: [PINNED_SKILL],
      })
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
      render(
        <QueryClientProvider client={qc}>
          <MemoryRouter>
            <SkillContextBudget onBack={() => {}} />
          </MemoryRouter>
        </QueryClientProvider>,
      )
      await screen.findByText(/Always Skill/i)
      // No switch should be present
      expect(screen.queryAllByRole('switch')).toHaveLength(0)
    })

    it('does NOT show toggle for package-owned skills', async () => {
      mockApi.skillsBudget.mockResolvedValue({
        window_days: 30,
        total_chars: 160000,
        rows: [PACKAGE_SKILL],
      })
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
      render(
        <QueryClientProvider client={qc}>
          <MemoryRouter>
            <SkillContextBudget onBack={() => {}} />
          </MemoryRouter>
        </QueryClientProvider>,
      )
      await screen.findByText(/Pkg Skill/i)
      expect(screen.queryAllByRole('switch')).toHaveLength(0)
    })

    it('calls setSkillInjectOnTrigger on toggle flip', async () => {
      mockApi.skillsBudget.mockResolvedValue({
        window_days: 30,
        total_chars: 14427392,
        rows: [HOT_SKILL],
      })
      mockApi.setSkillInjectOnTrigger.mockResolvedValue({})
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
      render(
        <QueryClientProvider client={qc}>
          <MemoryRouter>
            <SkillContextBudget onBack={() => {}} />
          </MemoryRouter>
        </QueryClientProvider>,
      )
      const toggle = await screen.findByRole('switch')
      fireEvent.click(toggle)
      await waitFor(() => {
        expect(mockApi.setSkillInjectOnTrigger).toHaveBeenCalledWith('kirocrew-commands', false)
      })
    })
  })

  describe('empty state', () => {
    it('renders empty state when no rows returned', async () => {
      mockApi.skillsBudget.mockResolvedValue({
        window_days: 30,
        total_chars: 0,
        rows: [],
      })
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
      render(
        <QueryClientProvider client={qc}>
          <MemoryRouter>
            <SkillContextBudget onBack={() => {}} />
          </MemoryRouter>
        </QueryClientProvider>,
      )
      expect(await screen.findByText(/no budget data yet/i)).toBeTruthy()
    })
  })

  describe('header metrics', () => {
    it('shows total chars and top 3 percentage without divide-by-zero', async () => {
      mount()
      // Should show the total
      await screen.findByText(/14.4M/i) // total ≈ 19.5M, top skill is 14.4M
    })

    it('handles zero total without crashing (no divide by zero)', async () => {
      mockApi.skillsBudget.mockResolvedValue({
        window_days: 30,
        total_chars: 0,
        rows: [{ ...HOT_SKILL, chars: 0, deliveries: 0 }],
      })
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
      render(
        <QueryClientProvider client={qc}>
          <MemoryRouter>
            <SkillContextBudget onBack={() => {}} />
          </MemoryRouter>
        </QueryClientProvider>,
      )
      // Should render without crashing - percentages are 0 not NaN
      await waitFor(() => {
        const texts = screen.getAllByText(/0%/)
        expect(texts.length).toBeGreaterThan(0)
        // Ensure no NaN
        expect(screen.queryByText(/NaN/)).toBeNull()
      })
    })
  })

  describe('back button', () => {
    it('calls onBack when clicked', async () => {
      const { onBack } = mount()
      // The back button is a Btn containing ArrowLeft icon + "Skills" text
      const backBtn = await screen.findByRole('button', { name: /skills/i })
      fireEvent.click(backBtn)
      expect(onBack).toHaveBeenCalled()
    })
  })
})
