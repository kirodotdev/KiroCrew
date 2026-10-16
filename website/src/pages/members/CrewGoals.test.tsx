import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import type { MemberGoals } from '../../api/client'

const memberGoals = vi.hoisted(() => ({
  get: vi.fn(),
  setGoal: vi.fn(),
  removeGoal: vi.fn(),
  restoreGoal: vi.fn(),
  setKeyResults: vi.fn(),
  removeKeyResult: vi.fn(),
  restoreKeyResult: vi.fn(),
  setSchedules: vi.fn(),
}))

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return { ...actual, api: { memberGoals } }
})

const touch = vi.hoisted(() => ({ on: false }))
vi.mock('../../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => touch.on }))

import CrewGoalsPane, { GOAL_COLORS, goalColor, keyResultProgress } from './CrewGoals'
import { ProfilePagePushContext } from './CrewProfilePanel'

const NOW = 1_800_000_000

const goal = (n: number, text: string) => ({
  id: `g0000000${n}`, number: n, text, created_at: NOW, updated_at: NOW, updated_by: 'user' as const,
})

const record = (over: Partial<MemberGoals> = {}): MemberGoals => ({
  slug: 'scout',
  member: 'scout',
  goals: [goal(1, 'Keep the release notes current')],
  key_results: [
    {
      id: 'k00000001',
      text: 'PRs with a description',
      target: '27',
      current: '19, the rest wait on the template change',
      updated_at: NOW - 3600,
      readings: [{ value: '12', at: NOW - 7200 }, { value: '19, the rest wait on the template change', at: NOW - 3600 }],
      goal_ids: ['g00000001'],
    },
    { id: 'k00000002', text: 'Notes published', target: 'yes', current: '', updated_at: 0, readings: [], goal_ids: [] },
  ],
  schedules: { time: '09:00', timezone: '', daily: false, weekly: false, weekday: 1, monthly: false, month_day: 1, heartbeat: false, interval: 30 },
  reports: [{ kind: 'weekly', slug: 'week-41', title: 'Week 41', at: NOW - 86400 }],
  removed_key_result: null,
  removed_goal: null,
  limits: {
    goal_text_max_chars: 2000,
    key_result_text_max_chars: 200,
    key_result_value_max_chars: 40,
    key_result_reading_max_chars: 120,
  },
  ...over,
})

beforeEach(() => {
  for (const fn of Object.values(memberGoals)) fn.mockReset()
})

describe('keyResultProgress', () => {
  it('reads numbers as a fraction of the target and a word target as reached or not', () => {
    expect(keyResultProgress({ current: '19', target: '27' })).toBeCloseTo(19 / 27)
    expect(keyResultProgress({ current: '40', target: '27' })).toBe(1)
    expect(keyResultProgress({ current: 'yes', target: 'Yes' })).toBe(1)
    expect(keyResultProgress({ current: 'no', target: 'yes' })).toBe(0)
    expect(keyResultProgress({ current: '', target: '27' })).toBeNull()
  })
})

describe('CrewGoalsPane', () => {
  it('shows the goal, each milestone with its latest reading in words, and the reports', async () => {
    memberGoals.get.mockResolvedValue(record())
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    expect(await screen.findByTestId('crew-goals-long-term-text')).toHaveTextContent('Keep the release notes current')
    const section = screen.getByTestId('crew-goals-key-results')
    expect(within(section).getByText('Milestones')).toBeInTheDocument()
    expect(within(section).getAllByTestId('goal-key-result')).toHaveLength(2)
    // A reading is free text, never a bar the record does not hold.
    expect(within(section).getByText('19, the rest wait on the template change')).toBeInTheDocument()
    expect(screen.queryByRole('progressbar')).toBeNull()
    expect(within(section).getAllByText(/^Aiming for · /).length).toBeGreaterThan(0)
    const report = within(screen.getByTestId('crew-goals-reports')).getByRole('link', { name: /^Week 41/ })
    expect(report).toHaveAttribute('href', '/artifacts/week-41')
    // The row names the report the same way its switch does.
    expect(report).toHaveTextContent(/Weekly report · /)
  })

  it('shows no short-term goals: the work ledger says what the crewmate is doing now', async () => {
    memberGoals.get.mockResolvedValue(record())
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    await screen.findByTestId('crew-goals-key-results')
    expect(screen.queryByTestId('crew-goals-short-term')).toBeNull()
    expect(screen.queryByTestId('crew-goals-undo')).toBeNull()
  })

  it('keeps the first page to state: no switches, editors or milestone actions', async () => {
    memberGoals.get.mockResolvedValue(record())
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    await screen.findByTestId('crew-goals-key-results')
    expect(screen.queryAllByRole('switch')).toHaveLength(0)
    expect(screen.queryByTestId('crew-goals-long-term-input')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Edit milestone' })).toBeNull()
    expect(screen.queryByTestId('crew-goals-kr-remove')).toBeNull()
    // Every card's label carries its icon.
    expect(screen.getAllByTestId('goal-section-icon')).toHaveLength(4)
    // Every label leads with a tinted icon tile.
    for (const id of ['crew-goals-long-term', 'crew-goals-key-results', 'crew-goals-checkins', 'crew-goals-reports']) {
      const icon = within(screen.getByTestId(id)).getByTestId('goal-section-icon')
      expect(icon.nextElementSibling?.textContent).toMatch(/\S/)
      expect(icon.className).toMatch(/bg-/)
    }
    // Each check-in shows whether it is on, and when it runs.
    expect(screen.getByTestId('crew-goals-checkin-daily')).toHaveAttribute('data-on', 'false')
    expect(screen.getByTestId('crew-goals-checkin-daily')).toHaveTextContent('Off')
  })

  it('shows when an on check-in runs', async () => {
    const base = record()
    memberGoals.get.mockResolvedValue(record({ schedules: { ...base.schedules, daily: true, monthly: true, month_day: 3 } }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    expect(await screen.findByTestId('crew-goals-checkin-daily')).toHaveTextContent('09:00')
    expect(screen.getByTestId('crew-goals-checkin-daily')).toHaveAttribute('data-on', 'true')
    expect(screen.getByTestId('crew-goals-checkin-monthly')).toHaveTextContent('Day 3 · 09:00')
    expect(screen.getByTestId('crew-goals-checkin-weekly')).toHaveTextContent('Off')
  })

  it('opens a milestone page with its earlier updates and its actions, and comes back', async () => {
    memberGoals.get.mockResolvedValue(record())
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const section = await screen.findByTestId('crew-goals-key-results')
    fireEvent.click(within(section).getAllByTestId('goal-key-result-open')[0])
    const page = screen.getByTestId('crew-goals-kr-page')
    expect(within(page).getByTestId('crew-goals-kr-current')).toHaveTextContent('19, the rest wait on the template change')
    const history = within(page).getByRole('list', { name: 'Earlier updates' })
    expect(within(history).getByText('12')).toBeInTheDocument()
    // The list carries a visible heading, not only an accessible name.
    expect(within(page).getByText('Earlier updates', { selector: 'div' })).toBeInTheDocument()
    // The actions are labelled text buttons, not bare icons.
    expect(screen.getByRole('button', { name: 'Edit milestone' })).toHaveTextContent('Edit milestone')
    expect(screen.getByTestId('crew-goals-kr-remove')).toHaveTextContent('Remove milestone')
    fireEvent.click(screen.getByTestId('crew-goals-back'))
    expect(screen.queryByTestId('crew-goals-kr-page')).toBeNull()
    expect(screen.getByTestId('crew-goals-key-results')).toBeInTheDocument()
  })

  it('removes a milestone from its page, returns to the first page and offers an Undo', async () => {
    const full = record()
    const removed = record({
      key_results: full.key_results.slice(1),
      removed_key_result: { id: 'k00000001', text: 'PRs with a description', at: NOW },
    })
    memberGoals.get.mockResolvedValue(full)
    memberGoals.removeKeyResult.mockResolvedValue(removed)
    memberGoals.restoreKeyResult.mockResolvedValue(full)
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    fireEvent.click((await screen.findAllByTestId('goal-key-result-open'))[0])
    fireEvent.click(screen.getByTestId('crew-goals-kr-remove'))
    await waitFor(() => expect(memberGoals.removeKeyResult).toHaveBeenCalledWith('scout', 'scout', 'k00000001'))
    expect(memberGoals.setKeyResults).not.toHaveBeenCalled()
    const section = await screen.findByTestId('crew-goals-key-results')
    expect(await within(section).findByText('Milestone removed')).toBeInTheDocument()
    fireEvent.click(within(section).getByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(memberGoals.restoreKeyResult).toHaveBeenCalledWith('scout', 'scout', 'k00000001'))
    await waitFor(() => expect(within(section).queryByTestId('crew-goals-kr-undo')).toBeNull())
    expect(within(section).getAllByTestId('goal-key-result')).toHaveLength(2)
  })

  it('shows only the title and aim for a milestone with no reading', async () => {
    memberGoals.get.mockResolvedValue(record())
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const section = await screen.findByTestId('crew-goals-key-results')
    expect(within(section).queryByText('Not checked yet')).toBeNull()
    expect(within(section).getAllByText(/^Aiming for · /).length).toBeGreaterThan(0)
  })

  const openCheckIns = async () => {
    fireEvent.click(await screen.findByTestId('crew-goals-checkins-open'))
    return screen.getByTestId('crew-goals-checkins-page')
  }

  it('keeps every check-in switch disabled while there is no goal', async () => {
    memberGoals.get.mockResolvedValue(record({ goals: [], key_results: [] }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const page = await openCheckIns()
    const switches = within(page).getAllByRole('switch')
    expect(switches).toHaveLength(4)
    for (const sw of switches) expect(sw).toHaveAttribute('aria-disabled', 'true')
  })

  it('shows the heartbeat as a full-width row under the three tiles, off by default', async () => {
    memberGoals.get.mockResolvedValue(record())
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const row = await screen.findByTestId('crew-goals-checkin-heartbeat')
    expect(row).toHaveAttribute('data-on', 'false')
    expect(row).toHaveTextContent('Heartbeat')
    expect(row).toHaveTextContent('Every 30 min, finds work on its own')
    expect(row).toHaveTextContent('Off')
    // A sibling of the three-tile grid, not a fourth tile inside it.
    const grid = screen.getByTestId('crew-goals-checkin-daily').parentElement
    expect(grid).not.toContainElement(row)
    expect(screen.getByTestId('crew-goals-checkins')).toContainElement(row)
  })

  it('shows an on heartbeat with its interval', async () => {
    const base = record()
    memberGoals.get.mockResolvedValue(record({ schedules: { ...base.schedules, heartbeat: true, interval: 120 } }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const row = await screen.findByTestId('crew-goals-checkin-heartbeat')
    expect(row).toHaveAttribute('data-on', 'true')
    expect(row).toHaveTextContent('Every 2 hr, finds work on its own')
    expect(row).toHaveTextContent('Running')
  })

  it('sets the heartbeat and its interval on the check-ins page, which names the cost', async () => {
    const base = record()
    memberGoals.get.mockResolvedValue(base)
    memberGoals.setSchedules.mockResolvedValue(record({ schedules: { ...base.schedules, heartbeat: true } }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const page = await openCheckIns()
    const settings = within(page).getByTestId('crew-goals-heartbeat-settings')
    expect(settings).toHaveTextContent('Each wake uses credits.')
    expect(within(settings).queryByRole('combobox', { name: 'How often' })).toBeNull()
    fireEvent.click(within(settings).getByRole('switch', { name: 'Heartbeat' }))
    await waitFor(() => expect(memberGoals.setSchedules).toHaveBeenCalledWith('scout', 'scout', { heartbeat: true }))
    expect(await within(settings).findByRole('combobox', { name: 'How often' })).toBeEnabled()
  })

  it('changes the heartbeat interval through its picker', async () => {
    touch.on = true
    try {
      const base = record()
      const on = record({ schedules: { ...base.schedules, heartbeat: true } })
      memberGoals.get.mockResolvedValue(on)
      memberGoals.setSchedules.mockResolvedValue(record({ schedules: { ...on.schedules, interval: 60 } }))
      renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
      const page = await openCheckIns()
      const picker = within(within(page).getByTestId('crew-goals-heartbeat-settings')).getByRole('combobox', { name: 'How often' })
      expect(Array.from((picker as HTMLSelectElement).options).map(o => o.value)).toEqual(['15', '30', '60', '120', '240'])
      fireEvent.change(picker, { target: { value: '60' } })
      await waitFor(() => expect(memberGoals.setSchedules).toHaveBeenCalledWith('scout', 'scout', { interval: 60 }))
    } finally {
      touch.on = false
    }
  })

  it('shows the weekday and day-of-month pickers only under their switch while it is on', async () => {
    const off = record()
    memberGoals.get.mockResolvedValue(off)
    const { unmount } = renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    await openCheckIns()
    expect(screen.queryByRole('combobox', { name: 'Day of the week' })).toBeNull()
    expect(screen.queryByRole('combobox', { name: 'Day of the month' })).toBeNull()
    unmount()
    memberGoals.get.mockResolvedValue(record({ schedules: { ...off.schedules, weekly: true } }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    await openCheckIns()
    expect(await screen.findByRole('combobox', { name: 'Day of the week' })).toBeEnabled()
    expect(screen.queryByRole('combobox', { name: 'Day of the month' })).toBeNull()
  })

  it('disables both pickers when there is no goal, whatever the switches say', async () => {
    const base = record()
    memberGoals.get.mockResolvedValue(record({ goals: [], schedules: { ...base.schedules, weekly: true, monthly: true } }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    await openCheckIns()
    expect(await screen.findByRole('combobox', { name: 'Day of the week' })).toBeDisabled()
    expect(screen.getByRole('combobox', { name: 'Day of the month' })).toBeDisabled()
  })

  it('keeps the goal draft when its save fails and returns to the first page once a save lands', async () => {
    memberGoals.get.mockResolvedValue(record())
    memberGoals.setGoal.mockRejectedValueOnce(new Error('boom'))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    fireEvent.click(await screen.findByTestId('crew-goals-goal'))
    fireEvent.change(screen.getByTestId('crew-goals-long-term-input'), { target: { value: 'A new goal' } })
    fireEvent.click(screen.getByTestId('crew-goals-long-term-save'))
    expect(await screen.findByTestId('crew-goals-save-error')).toBeInTheDocument()
    expect(screen.getByTestId('crew-goals-long-term-input')).toHaveValue('A new goal')
    memberGoals.setGoal.mockResolvedValueOnce(record({ goals: [goal(1, 'A new goal')] }))
    fireEvent.click(screen.getByTestId('crew-goals-long-term-save'))
    await waitFor(() => expect(screen.queryByTestId('crew-goals-long-term-input')).toBeNull())
    expect(await screen.findByTestId('crew-goals-long-term-text')).toHaveTextContent('A new goal')
  })

  it('keeps an unsaved draft when its goal is removed elsewhere, until Discard', async () => {
    memberGoals.get.mockResolvedValue(record())
    memberGoals.setGoal.mockRejectedValueOnce(new Error('missing'))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    fireEvent.click(await screen.findByTestId('crew-goals-goal'))
    fireEvent.change(screen.getByTestId('crew-goals-long-term-input'), { target: { value: 'My unsaved words' } })
    // Another tab removed the goal: the refused save refetches a record without it.
    memberGoals.get.mockResolvedValue(record({ goals: [], removed_goal: { id: 'g00000001', text: 'Keep the release notes current', at: NOW } }))
    fireEvent.click(screen.getByTestId('crew-goals-long-term-save'))
    expect(await screen.findByTestId('crew-goals-page-gone')).toBeInTheDocument()
    expect(screen.getByTestId('crew-goals-gone-draft')).toHaveValue('My unsaved words')
    fireEvent.click(screen.getByTestId('crew-goals-gone-discard'))
    await waitFor(() => expect(screen.queryByTestId('crew-goals-page-gone')).toBeNull())
  })

  it('saves a goal by its id against the text it started editing from', async () => {
    memberGoals.get.mockResolvedValue(record())
    memberGoals.setGoal.mockResolvedValue(record({ goals: [goal(1, 'Mine')] }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    fireEvent.click(await screen.findByTestId('crew-goals-goal'))
    fireEvent.change(screen.getByTestId('crew-goals-long-term-input'), { target: { value: 'Mine' } })
    fireEvent.click(screen.getByTestId('crew-goals-long-term-save'))
    await waitFor(() => expect(memberGoals.setGoal).toHaveBeenCalledWith('scout', 'scout', 'Mine', 'g00000001', 'Keep the release notes current'))
  })

  it('saves a milestone edit against the set it started from, keyed by id', async () => {
    const a = { id: 'k0000000a', text: 'A', target: '1', current: '', updated_at: 0, readings: [], goal_ids: [] }
    const b = { id: 'k0000000b', text: 'B', target: '2', current: '', updated_at: 0, readings: [], goal_ids: [] }
    const c = { id: 'k0000000c', text: 'C', target: '3', current: '', updated_at: 0, readings: [], goal_ids: [] }
    memberGoals.get.mockResolvedValue(record({ key_results: [a, b, c] }))
    memberGoals.setKeyResults.mockRejectedValue(new Error('changed_meanwhile'))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    fireEvent.click((await screen.findAllByTestId('goal-key-result-open'))[1])
    fireEvent.click(screen.getByRole('button', { name: 'Edit milestone' }))
    fireEvent.change(screen.getByDisplayValue('B'), { target: { value: 'B2' } })
    const getsBeforeSave = memberGoals.get.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(memberGoals.setKeyResults).toHaveBeenCalledTimes(1))
    const [, , items, expected] = memberGoals.setKeyResults.mock.calls[0]
    // Each item names its milestone by id, so the reworded B keeps its readings.
    expect(items).toEqual([
      { id: 'k0000000a', text: 'A', target: '1' },
      { id: 'k0000000b', text: 'B2', target: '2' },
      { id: 'k0000000c', text: 'C', target: '3' },
    ])
    expect(expected).toEqual([{ text: 'A', target: '1' }, { text: 'B', target: '2' }, { text: 'C', target: '3' }])
    // The refused save re-reads the record, so the next try compares against
    // what is stored now; the draft is kept on its page.
    await waitFor(() => expect(memberGoals.get.mock.calls.length).toBeGreaterThan(getsBeforeSave))
    expect(screen.getByDisplayValue('B2')).toBeInTheDocument()
    expect(screen.getByTestId('crew-goals-save-error')).toBeInTheDocument()
  })

  it('turns on one check-in at a time', async () => {
    memberGoals.get.mockResolvedValue(record())
    memberGoals.setSchedules.mockResolvedValue(record())
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const page = await openCheckIns()
    fireEvent.click(within(page).getByRole('switch', { name: 'Weekly report' }))
    await waitFor(() => expect(memberGoals.setSchedules).toHaveBeenCalledWith('scout', 'scout', { weekly: true }))
  })

  it('sizes the touch day pickers on their wrapper, so the chevron sits inside the box', async () => {
    touch.on = true
    try {
      const base = record()
      memberGoals.get.mockResolvedValue(record({ schedules: { ...base.schedules, weekly: true, monthly: true } }))
      renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
      const page = await openCheckIns()
      const pickers = Array.from(page.querySelectorAll('select'))
      expect(pickers).toHaveLength(2)
      for (const sel of pickers) {
        expect(sel.parentElement?.style.width).toBe('11rem')
        expect(sel.className).not.toMatch(/\bh-8\b/)
        expect(sel.className).not.toMatch(/\bw-44\b/)
      }
    } finally {
      touch.on = false
    }
  })

  it('pushes its pages onto the profile card through the card host', async () => {
    memberGoals.get.mockResolvedValue(record())
    const host = document.createElement('div')
    document.body.appendChild(host)
    const open = vi.fn()
    const close = vi.fn()
    renderWithProviders(
      <ProfilePagePushContext.Provider value={{ open, close, host }}>
        <CrewGoalsPane slug="scout" member="scout" name="Scout" />
      </ProfilePagePushContext.Provider>,
    )
    fireEvent.click((await screen.findAllByTestId('goal-key-result-open'))[0])
    await waitFor(() => expect(open).toHaveBeenCalledWith('PRs with a description', expect.any(Function)))
    // The page renders into the card's host; the first page stays under it.
    expect(within(host).getByTestId('crew-goals-kr-page')).toBeInTheDocument()
    expect(screen.getByTestId('crew-goals-pane')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-goals-back')).toBeNull()
    host.remove()
  })

  it('closes a pushed goal page once its goal is removed, without re-pushing it as a new goal', async () => {
    const full = record()
    memberGoals.get.mockResolvedValue(full)
    memberGoals.removeGoal.mockResolvedValue(record({ goals: [], removed_goal: { id: 'g00000001', text: 'Keep the release notes current', at: NOW } }))
    const host = document.createElement('div')
    document.body.appendChild(host)
    const open = vi.fn()
    const close = vi.fn()
    renderWithProviders(
      <ProfilePagePushContext.Provider value={{ open, close, host }}>
        <CrewGoalsPane slug="scout" member="scout" name="Scout" />
      </ProfilePagePushContext.Provider>,
    )
    fireEvent.click(await screen.findByTestId('crew-goals-goal'))
    await waitFor(() => expect(open).toHaveBeenCalledTimes(1))
    fireEvent.click(within(host).getByTestId('crew-goals-goal-remove'))
    await waitFor(() => expect(close).toHaveBeenCalled())
    expect(open).toHaveBeenCalledTimes(1)
    expect(await screen.findByTestId('crew-goals-goal-undo-row')).toBeInTheDocument()
    host.remove()
  })

  it('lists every goal with a coloured dot that cycles past five', async () => {
    const goals = Array.from({ length: 7 }, (_, i) => goal(i + 1, `Goal text ${i + 1}`))
    memberGoals.get.mockResolvedValue(record({ goals }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const list = await screen.findByTestId('crew-goals-goal-list')
    const rows = within(list).getAllByTestId('crew-goals-goal')
    expect(rows).toHaveLength(7)
    const circles = within(list).getAllByTestId('goal-number')
    expect(circles.map(c => c.dataset.goalNumber)).toEqual(['1', '2', '3', '4', '5', '6', '7'])
    // A dot only: no numeral drawn.
    expect(circles.every(c => c.textContent === '')).toBe(true)
    expect(circles[0]).toHaveAccessibleName('Goal 1')
    expect(circles[0].style.background).toBe(GOAL_COLORS[0])
    // Goal 6 reuses goal 1's colour.
    expect(circles[5].style.background).toBe(GOAL_COLORS[0])
    expect(goalColor(5)).toBe(GOAL_COLORS[0])
    // No ok/warn hue among the goal colours.
    expect(GOAL_COLORS.join(' ')).not.toMatch(/--(ok|warn|tile-green|tile-orange)/)
    // The card itself is not a button; "+" is, and there is no cap hiding it.
    expect(screen.getByTestId('crew-goals-long-term').tagName).toBe('SECTION')
    expect(screen.getByRole('button', { name: 'Add a goal' })).toBeInTheDocument()
  })

  it("shows each milestone's linked goals as their dots, and nothing when none is linked", async () => {
    memberGoals.get.mockResolvedValue(record())
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const [first, second] = await screen.findAllByTestId('goal-key-result')
    const linked = within(first).getByTestId('goal-key-result-goals')
    const circle = within(linked).getByTestId('goal-number')
    expect(circle).toHaveAccessibleName('Goal 1')
    expect(circle.style.background).toBe(GOAL_COLORS[0])
    expect(circle).toHaveAttribute('title', 'Keep the release notes current')
    expect(within(second).queryByTestId('goal-key-result-goals')).toBeNull()
  })

  it('opens an empty editor from "+" and adds the goal without an id', async () => {
    memberGoals.get.mockResolvedValue(record())
    memberGoals.setGoal.mockResolvedValue(record({ goals: [goal(1, 'Keep the release notes current'), goal(2, 'Hire')] }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add a goal' }))
    const input = screen.getByTestId('crew-goals-long-term-input')
    expect(input).toHaveValue('')
    expect(screen.getByTestId('crew-goals-long-term-save')).toBeDisabled()
    expect(screen.queryByTestId('crew-goals-goal-remove')).toBeNull()
    expect(screen.getByTestId('crew-goals-new-goal-intro')).toHaveTextContent('A goal is what Scout works toward')
    fireEvent.change(input, { target: { value: 'Hire' } })
    fireEvent.click(screen.getByTestId('crew-goals-long-term-save'))
    await waitFor(() => expect(memberGoals.setGoal).toHaveBeenCalledWith('scout', 'scout', 'Hire', undefined, undefined))
    expect(await screen.findAllByTestId('crew-goals-goal')).toHaveLength(2)
  })

  it("opens a goal's page with its linked milestones, removes it and offers an Undo", async () => {
    const full = record()
    const removed = record({
      goals: [],
      key_results: full.key_results.map(k => ({ ...k, goal_ids: [] })),
      removed_goal: { id: 'g00000001', text: 'Keep the release notes current', at: NOW },
    })
    memberGoals.get.mockResolvedValue(full)
    memberGoals.removeGoal.mockResolvedValue(removed)
    memberGoals.restoreGoal.mockResolvedValue(full)
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    fireEvent.click(await screen.findByTestId('crew-goals-goal'))
    const page = screen.getByTestId('crew-goals-goal-page')
    expect(within(page).queryByTestId('crew-goals-new-goal-intro')).toBeNull()
    const milestones = within(page).getByTestId('crew-goals-goal-milestones')
    expect(within(milestones).getAllByTestId('crew-goals-goal-milestone')).toHaveLength(1)
    expect(within(milestones).getByText('PRs with a description')).toBeInTheDocument()
    fireEvent.click(within(page).getByTestId('crew-goals-goal-remove'))
    await waitFor(() => expect(memberGoals.removeGoal).toHaveBeenCalledWith('scout', 'scout', 'g00000001'))
    const card = await screen.findByTestId('crew-goals-long-term')
    await waitFor(() => expect(screen.queryByTestId('crew-goals-goal-page')).toBeNull())
    expect(await within(card).findByText('Goal removed')).toBeInTheDocument()
    fireEvent.click(within(card).getByTestId('crew-goals-goal-undo'))
    await waitFor(() => expect(memberGoals.restoreGoal).toHaveBeenCalledWith('scout', 'scout', 'g00000001'))
    expect(await screen.findAllByTestId('crew-goals-goal')).toHaveLength(1)
  })

  it('shows no Undo for a removal made before the tab was opened', async () => {
    memberGoals.get.mockResolvedValue(record({
      removed_goal: { id: 'g00000009', text: 'Old goal', at: NOW },
      removed_key_result: { id: 'k00000009', text: 'Old milestone', at: NOW },
    }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    await screen.findAllByTestId('crew-goals-goal')
    expect(screen.queryByTestId('crew-goals-goal-undo-row')).toBeNull()
    expect(screen.queryByTestId('crew-goals-kr-undo-row')).toBeNull()
  })

  it('asks the crewmate in its chat to track something, as a visible message', async () => {
    memberGoals.get.mockResolvedValue(record())
    const onTrackInChat = vi.fn().mockResolvedValue('dispatched')
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" onTrackInChat={onTrackInChat} />)
    const button = await screen.findByTestId('crew-goals-track')
    expect(button).toHaveTextContent('Add something Scout should track')
    expect(button.textContent).not.toMatch(/ledger/i)
    // Opening the page sends nothing; the user's own words are sent on Send.
    fireEvent.click(button)
    expect(onTrackInChat).not.toHaveBeenCalled()
    expect(screen.getByTestId('crew-goals-track-send')).toBeDisabled()
    onTrackInChat.mockResolvedValueOnce('refused')
    fireEvent.change(screen.getByTestId('crew-goals-track-input'), { target: { value: 'The 0.11 release date' } })
    fireEvent.click(screen.getByTestId('crew-goals-track-send'))
    // A refused send says so and keeps the text for another try.
    expect(await screen.findByTestId('crew-goals-track-error')).toBeInTheDocument()
    expect(screen.getByTestId('crew-goals-track-input')).toHaveValue('The 0.11 release date')
    fireEvent.click(screen.getByTestId('crew-goals-track-send'))
    await waitFor(() => expect(onTrackInChat).toHaveBeenLastCalledWith('Please track this for me: The 0.11 release date'))
    await waitFor(() => expect(screen.queryByTestId('crew-goals-track-page')).toBeNull())
  })

  it.each(['goal', 'milestone', 'tracking'] as const)('keeps the %s draft without offering an error hand-off', async (kind) => {
    memberGoals.get.mockResolvedValue(record())
    memberGoals.setGoal.mockRejectedValue(new Error('save failed'))
    memberGoals.setKeyResults.mockRejectedValue(new Error('save failed'))
    const onTrackInChat = vi.fn().mockResolvedValue('refused')
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" onTrackInChat={onTrackInChat} />)
    let input: HTMLElement
    if (kind === 'goal') {
      fireEvent.click(await screen.findByTestId('crew-goals-goal'))
      input = screen.getByTestId('crew-goals-long-term-input')
    } else if (kind === 'milestone') {
      fireEvent.click((await screen.findAllByTestId('goal-key-result-open'))[0])
      fireEvent.click(screen.getByRole('button', { name: 'Edit milestone' }))
      input = screen.getByLabelText('Milestone')
      fireEvent.change(screen.getByLabelText('Aiming for'), { target: { value: '31' } })
    } else {
      fireEvent.click(await screen.findByTestId('crew-goals-track'))
      input = screen.getByTestId('crew-goals-track-input')
    }
    fireEvent.change(input, { target: { value: 'Keep this unsaved text' } })
    fireEvent.click(kind === 'tracking' ? screen.getByTestId('crew-goals-track-send') : screen.getByRole('button', { name: 'Save' }))
    const notice = await screen.findByTestId(kind === 'tracking' ? 'crew-goals-track-error' : 'crew-goals-save-error')
    expect(within(notice).queryByRole('button', { name: 'Ask the agent' })).toBeNull()
    expect(input).toHaveValue('Keep this unsaved text')
    if (kind === 'milestone') expect(screen.getByLabelText('Aiming for')).toHaveValue('31')
  })

  it('offers an error hand-off for a failed check-in change', async () => {
    memberGoals.get.mockResolvedValue(record())
    memberGoals.setSchedules.mockRejectedValue(new Error('save failed'))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const page = await openCheckIns()
    fireEvent.click(within(page).getByRole('switch', { name: 'Weekly report' }))
    const notice = await within(page).findByTestId('crew-goals-save-error')
    expect(within(notice).getByRole('button', { name: 'Ask the agent' })).toBeEnabled()
    expect(within(page).getByRole('switch', { name: 'Weekly report' })).toHaveAttribute('aria-checked', 'false')
  })

  it.each(['goal', 'kr'] as const)('offers an error hand-off for a failed %s Undo', async (kind) => {
    const full = record()
    const removed = kind === 'goal'
      ? record({ goals: [], removed_goal: { id: 'g00000001', text: full.goals[0].text, at: NOW } })
      : record({ key_results: full.key_results.slice(1), removed_key_result: { id: 'k00000001', text: full.key_results[0].text, at: NOW } })
    memberGoals.get.mockResolvedValueOnce(full).mockResolvedValue(removed)
    memberGoals.removeGoal.mockResolvedValue(removed)
    memberGoals.removeKeyResult.mockResolvedValue(removed)
    memberGoals.restoreGoal.mockRejectedValue(new Error('undo failed'))
    memberGoals.restoreKeyResult.mockRejectedValue(new Error('undo failed'))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    fireEvent.click(kind === 'goal' ? await screen.findByTestId('crew-goals-goal') : (await screen.findAllByTestId('goal-key-result-open'))[0])
    fireEvent.click(screen.getByTestId(`crew-goals-${kind}-remove`))
    fireEvent.click(await screen.findByTestId(`crew-goals-${kind}-undo`))
    const notice = await screen.findByTestId('crew-goals-save-error')
    expect(within(notice).getByRole('button', { name: 'Ask the agent' })).toBeEnabled()
  })

  it('offers a hand-off for milestone removal but not while its editor holds a draft', async () => {
    memberGoals.get.mockResolvedValue(record())
    memberGoals.removeKeyResult.mockRejectedValue(new Error('remove failed'))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    fireEvent.click((await screen.findAllByTestId('goal-key-result-open'))[0])
    fireEvent.click(screen.getByTestId('crew-goals-kr-remove'))
    const notice = await screen.findByTestId('crew-goals-save-error')
    expect(within(notice).getByRole('button', { name: 'Ask the agent' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Edit milestone' }))
    fireEvent.change(screen.getByLabelText('Milestone'), { target: { value: 'Keep this unsaved milestone' } })
    expect(within(notice).queryByRole('button', { name: 'Ask the agent' })).toBeNull()
    expect(screen.getByLabelText('Milestone')).toHaveValue('Keep this unsaved milestone')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(within(notice).getByRole('button', { name: 'Ask the agent' })).toBeEnabled()
  })

  it('offers no track button without a thread to send to', async () => {
    memberGoals.get.mockResolvedValue(record())
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    await screen.findByTestId('crew-goals-checkins')
    expect(screen.queryByTestId('crew-goals-track')).toBeNull()
  })
})
