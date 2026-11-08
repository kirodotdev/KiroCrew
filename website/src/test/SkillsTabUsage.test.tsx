import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { pendingCreatedAt, sortByCreated, sortSkills } from '../pages/overview/skillSort'
import type { Skill } from '../types'

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

const NOW_S = Math.floor(Date.now() / 1000)

const skill = (key: string, deliveries: number | null, lastUsedAgoS: number | null): Skill => ({
  key,
  name: key,
  description: `${key} skill`,
  source: 'kirocrew',
  inject_on_trigger: true,
  size_bytes: 1000,
  deliveries,
  last_used_at: lastUsedAgoS === null ? null : NOW_S - lastUsedAgoS,
} as Skill)

/* Server order is alphabetical here on purpose, so a sort that silently keeps
   it would pass for "default" and fail for the other two. */
const ALPHA = skill('alpha', 2, 3 * 86400)
const BRAVO = skill('bravo', null, null)
const CHARLIE = skill('charlie', 12, 5 * 86400)
const DELTA = skill('delta', 2, 3600)

function mount(skills: Skill[]) {
  mockApi.skills.mockResolvedValue(skills)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><SkillsTab /></MemoryRouter>
    </QueryClientProvider>,
  )
}

const dated = (key: string, createdAt: number | null): Skill => ({ ...skill(key, null, null), created_at: createdAt })

const pending = (slug: string, createdAt: string | undefined) => ({
  slug, name: slug, description: `${slug} candidate`, has_scripts: false, created_at: createdAt,
})

async function openSort() {
  fireEvent.pointerDown(await screen.findByRole('combobox', { name: 'Sort skills' }), {
    pointerType: 'mouse', button: 0, ctrlKey: false,
  })
}

const pendingOrder = () => ['older', 'undated', 'newer']
  .map(n => [n, screen.getByText(n)] as const)
  .sort(([, a], [, b]) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1))
  .map(([n]) => n)

const rowOrder = () =>
  screen.getAllByRole('button', { name: /^Select / }).map(r => r.getAttribute('aria-label')?.replace('Select ', ''))

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.skill.mockResolvedValue({ name: 'x', content: '---\nname: x\n---\nbody' })
  mockApi.skillsPending.mockResolvedValue({ skills: [] })
  mockApi.setSkillInjectOnTrigger.mockResolvedValue({})
})

describe('usage on each Skills list row', () => {
  it('shows how often and how recently a skill was loaded', async () => {
    mount([CHARLIE])
    const row = await screen.findByRole('button', { name: 'Select Charlie' })
    /* Assert the content, not CLDR's exact wording: the relative-time form
       comes from Intl and shifts with the ICU version. */
    const usage = within(row).getByText(/^12× · /)
    expect(usage.textContent).toMatch(/5/)
    expect(usage.getAttribute('title')).toMatch(/^Loaded 12× · last loaded [^.]+\.$/)
  })

  it('gives a screen reader the count, which the row label alone would hide', async () => {
    mount([CHARLIE])
    const row = await screen.findByRole('button', { name: 'Select Charlie' })
    expect(row).toHaveAccessibleDescription(/^12× · /)
  })

  it('keeps each description when a skill key holds a space', async () => {
    /* `aria-describedby` splits on whitespace, so a raw `my skill` id names
       two ids that do not exist. `my-skill` beside it pins that the encoding
       keeps distinct keys on distinct ids: both rows show as "My Skill". */
    mount([skill('my skill', 3, 3600), skill('my-skill', 7, 3600)])
    const [spaced, hyphenated] = await screen.findAllByRole('button', { name: 'Select My Skill' })
    expect(spaced).toHaveAccessibleDescription(/^3× · /)
    expect(hyphenated).toHaveAccessibleDescription(/^7× · /)
  })

  it('shows nothing for a skill with no recorded use', async () => {
    mount([BRAVO])
    const row = await screen.findByRole('button', { name: 'Select Bravo' })
    expect(within(row).queryByText(/×/)).toBeNull()
    expect(row.getAttribute('aria-describedby')).toBeNull()
  })
})

describe('Skills list sort', () => {
  it('keeps the server order by default', async () => {
    mount([ALPHA, BRAVO, CHARLIE, DELTA])
    await screen.findByRole('button', { name: 'Select Alpha' })
    expect(rowOrder()).toEqual(['Alpha', 'Bravo', 'Charlie', 'Delta'])
  })

  it('reorders the list when Most used is picked', async () => {
    mount([ALPHA, BRAVO, CHARLIE, DELTA])
    fireEvent.pointerDown(await screen.findByRole('combobox', { name: 'Sort skills' }), {
      pointerType: 'mouse', button: 0, ctrlKey: false,
    })
    fireEvent.click(await screen.findByRole('option', { name: 'Most used' }))
    expect(rowOrder()).toEqual(['Charlie', 'Delta', 'Alpha', 'Bravo'])
  })

  it('offers the four orders', async () => {
    mount([ALPHA])
    fireEvent.pointerDown(await screen.findByRole('combobox', { name: 'Sort skills' }), {
      pointerType: 'mouse', button: 0, ctrlKey: false,
    })
    const options = await screen.findAllByRole('option')
    expect(options.map(o => o.textContent)).toEqual(['Default order', 'Most used', 'Recently used', 'Newest first'])
  })

  it('puts the newest recorded skill first and undated ones last, in server order', async () => {
    mount([
      dated('alpha', null), dated('bravo', 100), dated('charlie', null), dated('delta', 300), dated('echo', 200),
    ])
    await screen.findByRole('button', { name: 'Select Alpha' })
    await openSort()
    fireEvent.click(await screen.findByRole('option', { name: 'Newest first' }))
    expect(rowOrder()).toEqual(['Delta', 'Echo', 'Bravo', 'Alpha', 'Charlie'])
  })

  it('orders Pending review newest first too, and back again on Default order', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [
      pending('older', '2026-10-01T00:00:00Z'), pending('undated', undefined), pending('newer', '2026-10-02T00:00:00Z'),
    ] })
    mount([ALPHA])
    await screen.findByText('older')
    expect(pendingOrder()).toEqual(['older', 'undated', 'newer'])
    await openSort()
    fireEvent.click(await screen.findByRole('option', { name: 'Newest first' }))
    expect(pendingOrder()).toEqual(['newer', 'older', 'undated'])
    await openSort()
    fireEvent.click(await screen.findByRole('option', { name: 'Default order' }))
    expect(pendingOrder()).toEqual(['older', 'undated', 'newer'])
  })
})

describe('sortSkills', () => {
  const rows = [ALPHA, BRAVO, CHARLIE, DELTA]
  const keys = (list: Skill[]) => list.map(s => s.key)

  it('returns the server order untouched for default', () => {
    expect(sortSkills(rows, 'default')).toBe(rows)
  })

  it('puts the most loaded first and breaks a count tie by recency', () => {
    expect(keys(sortSkills(rows, 'used'))).toEqual(['charlie', 'delta', 'alpha', 'bravo'])
  })

  it('puts the latest use first and never-used skills last', () => {
    expect(keys(sortSkills(rows, 'recent'))).toEqual(['delta', 'alpha', 'charlie', 'bravo'])
  })

  it('keeps the server order among skills that tie', () => {
    const idle = [skill('zulu', null, null), skill('yankee', null, null)]
    expect(keys(sortSkills(idle, 'used'))).toEqual(['zulu', 'yankee'])
    expect(keys(sortSkills(idle, 'recent'))).toEqual(['zulu', 'yankee'])
  })

  it('does not reorder the array it was given', () => {
    const input = [...rows]
    sortSkills(input, 'used')
    expect(keys(input)).toEqual(keys(rows))
  })

  it('reads only the recorded creation time for Newest first', () => {
    // Usage must not leak into the order: the most used skill is undated here.
    const input = [{ ...CHARLIE, created_at: null }, { ...BRAVO, created_at: 50 }, { ...DELTA, created_at: 90 }]
    expect(keys(sortSkills(input, 'created'))).toEqual(['delta', 'bravo', 'charlie'])
  })
})

describe('sortByCreated', () => {
  it('treats a missing or unparseable pending time as undated, not oldest', () => {
    const rows = [
      { slug: 'bad', created_at: 'not a date' }, { slug: 'old', created_at: '2020-01-01T00:00:00Z' },
      { slug: 'none' }, { slug: 'new', created_at: '2026-01-01T00:00:00Z' },
    ]
    expect(sortByCreated(rows, pendingCreatedAt).map(r => r.slug)).toEqual(['new', 'old', 'bad', 'none'])
  })
})
