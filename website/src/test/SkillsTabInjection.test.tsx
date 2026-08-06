import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/* ── Mocks: must run before importing the component ── */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skill: vi.fn(),
  skillsPending: vi.fn(),
  setSkillInjectOnTrigger: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

vi.mock('../providers', () => ({
  useProvider: () => ({ labels: { pluginRegistryName: 'Packages' } }),
}))

vi.mock('../components/SkillDirectoryBrowser', () => ({
  default: () => <div data-testid="dir-browser">browser</div>,
}))

import SkillsTab from '../pages/overview/SkillsTab'

const BASE = {
  key: 'kirocrew-commands',
  name: 'kirocrew-commands',
  description: 'CLI reference',
  source: 'kirocrew',
  inject_on_trigger: true,
  size_bytes: 21728,
  deliveries: 616,
}

function mount(skills: Record<string, unknown>[]) {
  mockApi.skills.mockResolvedValue(skills)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><SkillsTab /></MemoryRouter>
    </QueryClientProvider>,
  )
}

const findToggle = () => screen.findByRole('switch', { name: /inject full content on match/i })

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.skill.mockResolvedValue({ name: 'x', content: '---\nname: x\n---\nbody' })
  mockApi.skillsPending.mockResolvedValue({ skills: [] })
  mockApi.setSkillInjectOnTrigger.mockResolvedValue({})
})

describe('injection control on the Skills page', () => {
  it('shows the real cost so the choice is informed rather than blind', async () => {
    mount([BASE])
    /* Assert the content, not CLDR's exact rendering: `fmtBytes` / `fmtCompact`
       go through Intl, whose compact and unit forms shift with the ICU version,
       so pinning the literal string here would be a flake waiting to happen. */
    const line = await screen.findByText(/delivered 616×/)
    expect(line.textContent).toMatch(/21(\.7)? ?kB/)
    expect(line.textContent).toMatch(/13/) // 616 × 21,728 = 13,384,448 chars
  })

  it('names the window instead of implying the count is all-time', async () => {
    /* The ledger ages entries out after 30 days, so an unqualified "delivered
       616×" — and especially an unqualified "never" — asserts more than the data
       supports. A skill heavily used two months ago has no entry at all. */
    mount([BASE])
    const line = await screen.findByText(/delivered 616×/)
    expect(line.textContent).toMatch(/30 days/)
  })

  it('keeps the size visible when the ledger has nothing, and scopes the claim', async () => {
    mount([{ ...BASE, deliveries: null }])
    const line = await screen.findByText(/not delivered in the last 30 days/i)
    expect(line.textContent).toMatch(/21(\.7)? ?kB/)
  })

  it('says the figure stopped moving once a skill is opted out', async () => {
    /* A pointer delivers no body, so the count freezes. A number that silently
       stopped updating would read as current and mislead the next decision. */
    mount([{ ...BASE, inject_on_trigger: false }])
    expect(await screen.findByText(/no longer counting/i)).toBeTruthy()
  })

  it('writes the opt-out when the toggle is switched off', async () => {
    mount([BASE])

    fireEvent.click(await findToggle())

    await waitFor(() =>
      expect(mockApi.setSkillInjectOnTrigger).toHaveBeenCalledWith('kirocrew-commands', false))
  })

  it('writes the opt-in when an opted-out skill is switched back on', async () => {
    mount([{ ...BASE, inject_on_trigger: false }])

    const toggle = await findToggle()
    expect(toggle.getAttribute('aria-checked')).toBe('false')
    fireEvent.click(toggle)

    await waitFor(() =>
      expect(mockApi.setSkillInjectOnTrigger).toHaveBeenCalledWith('kirocrew-commands', true))
  })

  it('surfaces a failed write instead of leaving the toggle looking applied', async () => {
    mockApi.setSkillInjectOnTrigger.mockRejectedValue(new Error('boom'))
    mount([BASE])

    fireEvent.click(await findToggle())

    expect(await screen.findByText(/could not update this skill/i)).toBeTruthy()
  })

  it('states what the ON position actually does', async () => {
    mount([BASE])
    expect(await screen.findByText(/whole SKILL\.md is added every time a trigger matches/i)).toBeTruthy()
  })

  it('states that OFF makes following the skill the agent’s choice', async () => {
    mount([{ ...BASE, inject_on_trigger: false }])
    expect(await screen.findByText(/only a one-line pointer is added/i)).toBeTruthy()
  })

  it('marks an opted-out skill in the list so 38 skills stay scannable', async () => {
    mount([{ ...BASE, inject_on_trigger: false }])
    expect(await screen.findByText('pointer')).toBeTruthy()
  })

  it('hides the control for a pinned skill, which the matcher never fires', async () => {
    mount([{ ...BASE, always: true }])
    await screen.findByTestId('dir-browser')
    expect(screen.queryByRole('switch', { name: /inject full content/i })).toBeNull()
  })

  it('hides the control for a source the dashboard cannot write', async () => {
    mount([{ ...BASE, source: 'package' }])
    await screen.findByTestId('dir-browser')
    expect(screen.queryByRole('switch', { name: /inject full content/i })).toBeNull()
  })

  it('hides the control for a skill outside the directory Kiro Crew owns', async () => {
    /* A `skills.extra_paths` skill still reports source `kirocrew`, but the
       writer refuses to rewrite it. Offering a toggle that always errors is
       worse than not offering one, so the gate follows the reported writability. */
    mount([{ ...BASE, owned: false }])
    await screen.findByTestId('dir-browser')
    expect(screen.queryByRole('switch', { name: /inject full content/i })).toBeNull()
  })

  it('still shows the control when the listing omits ownership (older backend)', async () => {
    const { owned: _drop, ...noOwned } = { ...BASE, owned: undefined }
    mount([noOwned])
    expect(await findToggle()).toBeTruthy()
  })
})
