import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mockApi = vi.hoisted(() => ({
  skillsBudget: vi.fn(),
  setSkillInjectOnTrigger: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import SkillContextBudget from '../pages/overview/SkillContextBudget'
import type { SkillBudgetRow } from '../types'

const base: SkillBudgetRow = {
  key: 'x',
  name: 'x',
  size_bytes: 1000,
  deliveries: 2,
  chars: 2000,
  inject_on_trigger: true,
  always: false,
  owned: true,
  source: 'kirocrew',
  idle_days: 0,
}

const LONG = 'Builds live, interactive UI inside a chat reply. '.repeat(8).trim()

function mount(rows: SkillBudgetRow[]) {
  mockApi.skillsBudget.mockResolvedValue({
    window_days: 30,
    total_chars: rows.reduce((s, r) => s + (r.chars ?? 0), 0),
    rows,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SkillContextBudget onBack={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** The row element holding a skill's display name. */
async function rowOf(name: RegExp) {
  const label = await screen.findByText(name)
  return label.closest('div.flex-1') as HTMLElement
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
})

describe('Context budget rows explain what each skill is', () => {
  it('shows a builtin skill its description and a Built-in tag', async () => {
    mount([{ ...base, key: 'living-ui', name: 'living-ui', origin: 'builtin', description: 'Build live UI in chat.' }])
    const row = await rowOf(/Living Ui/)
    expect(within(row).getByText('Build live UI in chat.')).toBeTruthy()
    expect(within(row).getByText('Built-in')).toBeTruthy()
    expect(within(row).queryByText('Yours')).toBeNull()
  })

  it('tags a user-added skill as Yours', async () => {
    mount([{ ...base, key: 'my-notes', name: 'my-notes', origin: 'user', description: 'My notes.' }])
    const row = await rowOf(/My Notes/)
    expect(within(row).getByText('Yours')).toBeTruthy()
  })

  it('tags a skill from an installed app as App, not Built-in', async () => {
    mount([{ ...base, key: 'app-helper', name: 'app-helper', origin: 'app', description: 'From an app.' }])
    const row = await rowOf(/App Helper/)
    expect(within(row).getByText('App')).toBeTruthy()
    expect(within(row).queryByText('Built-in')).toBeNull()
    expect(within(row).queryByText('Yours')).toBeNull()
  })

  it('renders no empty description line for a skill without one', async () => {
    mount([{ ...base, key: 'bare', name: 'bare', origin: 'user', description: '' }])
    const row = await rowOf(/^Bare$/)
    expect(row.querySelector('p')).toBeNull()
  })

  it('shows neither tag nor description when an older gateway omits both', async () => {
    mount([{ ...base, key: 'old', name: 'old' }])
    const row = await rowOf(/^Old$/)
    expect(within(row).queryByText('Built-in')).toBeNull()
    expect(within(row).queryByText('Yours')).toBeNull()
    expect(row.querySelector('p')).toBeNull()
  })

  it('clamps a long description behind a keyboard-reachable toggle', async () => {
    mount([{ ...base, key: 'long', name: 'long', origin: 'builtin', description: LONG }])
    const row = await rowOf(/^Long$/)
    const text = within(row).getByText(LONG)
    expect(text.className).toContain('line-clamp-2')
    const toggle = within(row).getByRole('button', { name: 'Show more' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle)
    expect(text.className).not.toContain('line-clamp-2')
    const less = within(row).getByRole('button', { name: 'Show less' })
    expect(less.getAttribute('aria-expanded')).toBe('true')
  })

  it('offers no toggle for a short description', async () => {
    mount([{ ...base, key: 'short', name: 'short', origin: 'builtin', description: 'Short.' }])
    const row = await rowOf(/^Short$/)
    expect(within(row).queryByRole('button')).toBeNull()
  })

  it('explains why a skill costs context above the table', async () => {
    mount([{ ...base, origin: 'builtin', description: 'd' }])
    expect(await screen.findByText(/full text is added to that chat/)).toBeTruthy()
  })

  it('describes every row in every group when many rows are listed', async () => {
    const rows: SkillBudgetRow[] = Array.from({ length: 40 }, (_, i) => ({
      ...base,
      key: `s${i}`,
      name: `s${i}`,
      // Spread rows across the counting, never-fired and pointer-only groups.
      chars: i % 3 === 1 ? 0 : 1000 + i,
      inject_on_trigger: i % 3 !== 2,
      origin: i % 2 === 0 ? 'builtin' : 'user',
      description: `Skill number ${i}.`,
    }))
    mount(rows)
    await screen.findByText('Skill number 0.')
    for (let i = 0; i < rows.length; i++) {
      expect(screen.getByText(`Skill number ${i}.`)).toBeTruthy()
    }
    expect(screen.getAllByText('Built-in')).toHaveLength(20)
    expect(screen.getAllByText('Yours')).toHaveLength(20)
  })
})
