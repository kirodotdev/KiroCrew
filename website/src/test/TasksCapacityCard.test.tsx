/**
 * System > Services > "Tasks & capacity".
 *
 * The card is the only dashboard surface that shows the durable task queue
 * and the effective concurrency the gateway is running under. These cases pin
 * the render to the `/api/tasks/summary` payload: depth by state, the oldest
 * wait, the cap in force stated AS A RELATIONSHIP to the user's own ceiling,
 * one row per waiting/retrying task or slot with its reason and age, the fold
 * control that opens the rest in place, the empty state, the no-store notice,
 * and the failure notice — whose reason is shown only when it is prose.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import type { TasksSummary } from '../api/tasks'

const tasksSummary = vi.fn<() => Promise<TasksSummary>>()

vi.mock('../api/client', () => ({
  api: {
    tasksSummary: () => tasksSummary(),
  },
}))

import TasksCapacityCard, { cardHealth, fmtAge, proseReason } from '../pages/system/TasksCapacityCard'

function summary(overrides: Partial<TasksSummary> = {}): TasksSummary {
  return {
    generated_at: 1_000,
    available: true,
    depth: { by_state: {}, queued: 0, waiting: 0, recovering: 0, running: 0, total: 0 },
    oldest_wait_secs: 0,
    lanes: {},
    degrade_reason: null,
    adaptive: null,
    slots: [],
    waiting: [],
    recovering: { tasks: [], task_attempts: 0, slots: [], ladder: [] },
    stalled: {},
    counts: {},
    stall_after_secs: 600,
    ...overrides,
  }
}

/**
 * The catalog glues the ceiling to the word before it with U+00A0. A wrapped
 * "lowered from your" leaving a bare "14" on the next line puts back exactly the
 * loose second number this line exists to remove, and the column is a third of
 * the card wide, so the break is reachable.
 */
const NBSP = ' '

/**
 * The counter's own label, told apart from the identically worded state badge a
 * row carries: only the label span is muted (`SectionBlock`), and both spell
 * "Stalled" once the list renders the row the count points at.
 */
const COUNTER = { selector: 'span.text-muted' } as const

function row(id: string, state: string, extra: Record<string, unknown> = {}) {
  return {
    id, kind: 'subagent', state, lane: 'web-a', session_key: 'web-a', parent_id: null, root_id: id,
    attempts: 1, generation: 1, next_run_at: null, deadline_at: null, lease_owner: null,
    lease_expires_at: null, wait: null, wait_reason: null, wait_since: null, wait_deadline_at: null,
    age_secs: 0, created_at: 0, updated_at: 0, terminal: false, ...extra,
  }
}

beforeEach(() => {
  tasksSummary.mockReset()
})

describe('TasksCapacityCard', () => {
  it('renders the empty state with a healthy badge and zero counts', async () => {
    tasksSummary.mockResolvedValue(summary())
    renderWithProviders(<TasksCapacityCard />)

    await screen.findByTestId('tasks-capacity-empty')
    expect(screen.getByText('Tasks & capacity')).toBeTruthy()
    expect(screen.getByText('Healthy')).toBeTruthy()
    expect(screen.getByText('Nothing is waiting or retrying.')).toBeTruthy()
    // No store notice when the store IS available.
    expect(screen.queryByText(/Queue history/)).toBeNull()
    // A payload with no lane still names the two lanes the wire publishes, with
    // no number in them: a label that appears ONLY here teaches a term the
    // populated card never uses again.
    expect(screen.getByText('Subagent runs').closest('div')!.textContent).toContain('—')
    expect(screen.getByText('Backend starts').closest('div')!.textContent).toContain('—')
    expect(screen.queryByText('Effective cap')).toBeNull()
  })

  it('names what a missing task store costs the reader, not the component that is absent', async () => {
    tasksSummary.mockResolvedValue(summary({ available: false }))
    renderWithProviders(<TasksCapacityCard />)
    await screen.findByText('Queue history isn\'t stored here; only live sessions are shown.')
    // "gateway" and "task store" are this codebase's words, not the reader's.
    const card = screen.getByTestId('tasks-capacity-card')
    expect(card.textContent).not.toMatch(/gateway|task store/i)
    // ... and NO health verdict sits over that notice: every input the healthy
    // and backlog rules read (`depth`, `oldest_wait_secs`) is a zero this payload
    // cannot fill, so "Healthy" would be a claim about a queue nothing recorded —
    // indistinguishable, to the reader, from "there is nothing to show".
    expect(screen.queryByTestId('tasks-capacity-health')).toBeNull()
    expect(screen.queryByText('Healthy')).toBeNull()
    expect(cardHealth(summary({ available: false }))).toBeNull()
  })

  it('keeps the verdict a store-less gateway CAN support: live stalls and the controller', async () => {
    // `stalled` comes from the live session monitor and `degrade_reason` from the
    // controller — both published with no store at all. Withholding the badge for
    // every store-less install would hide the one alarm that still has evidence.
    tasksSummary.mockResolvedValue(summary({
      available: false,
      stalled: { 'web-s': { reason: 'no_progress', since_ts: 0, age_secs: 700, evidence: ['no cpu delta'] } },
    }))
    const { unmount } = renderWithProviders(<TasksCapacityCard />)
    const badge = await screen.findByTestId('tasks-capacity-health')
    expect(badge.textContent).toBe('Stalled')
    expect(badge.className).toContain('text-danger')
    // The no-store notice still says what is missing, beside a verdict that stands.
    expect(screen.getByText('Queue history isn\'t stored here; only live sessions are shown.')).toBeTruthy()
    unmount()

    tasksSummary.mockReset()
    tasksSummary.mockResolvedValue(summary({ available: false, degrade_reason: 'adaptive_pause' }))
    renderWithProviders(<TasksCapacityCard />)
    expect((await screen.findByTestId('tasks-capacity-health')).textContent).toBe('Degraded')
  })

  it('shows depth, oldest wait, cap vs ceiling, the degrade reason and each wait with its age', async () => {
    tasksSummary.mockResolvedValue(summary({
      depth: {
        by_state: { queued: 12, running: 3, waiting_dependency: 1, waiting_input: 1, recovering: 2 },
        queued: 12, waiting: 2, recovering: 2, running: 3, total: 19,
      },
      oldest_wait_secs: 754,
      lanes: {
        subagents: { effective: 4, user_max: 8, running: 3 },
        spawn_gate: { effective: 2, user_max: 8 },
      },
      degrade_reason: 'loop_lag=410ms',
      waiting: [
        row('dep-1', 'waiting_dependency', {
          wait_reason: 'github:api rate limited', age_secs: 42,
          wait: { reason: 'github:api rate limited', since: 900, deadline_at: null, resume_kind: 'at_time', dependency_scope: 'github:api', cancel_semantics: 'task', tool_call_id: '' },
        }),
        row('inp-1', 'waiting_input', { wait_reason: 'sudo wants a password', age_secs: 3_700, session_key: 'dashboard:web-inp' }),
      ],
      recovering: {
        tasks: [
          row('rec-1', 'recovering', { attempts: 3, age_secs: 5, next_run_at: 1_000 + 90 }),
          row('park-1', 'retry_wait', { attempts: 2, age_secs: 4, next_run_at: 1_000 + 30 }),
        ],
        task_attempts: 3,
        slots: [{ key: 'web-z', age_secs: 12, evidence: ['retry in flight: infra'] }],
        ladder: [],
      },
      slots: [
        { key: 'web-z', classification: 'recovering', age_secs: 12, evidence: ['retry in flight: infra'] },
        { key: 'web-ok', classification: 'running', age_secs: 1, evidence: [] },
      ],
      stalled: { 'web-s': { reason: 'no_progress', since_ts: 0, age_secs: 700, evidence: [] } },
    }))
    renderWithProviders(<TasksCapacityCard />)

    // A stalled run outranks the degrade reason: the badge goes red, not amber.
    const badge = await screen.findByTestId('tasks-capacity-health')
    expect(badge.textContent).toBe('Stalled')
    expect(badge.className).toContain('text-danger')
    // Queue depth.
    const queued = screen.getByText('Waiting for a slot').closest('div')!
    expect(queued.textContent).toContain('12')
    expect(screen.getByText('Oldest wait').closest('div')!.textContent).toContain(fmtAge(754))
    // A lowered cap and the ceiling it came from share ONE line, joined by the
    // verb that relates them — never a bare cap stacked over a bare second
    // maximum, which reads as two limits and leaves the reader asking which one
    // is in force, and never "N of M" beside "3 running now".
    // The reason line is absent here because `loop_lag=410ms` is machine text.
    const [subagents, spawnGate] = screen.getAllByTestId('tasks-capacity-lane')
    const subagentLines = Array.from(subagents.querySelectorAll('span')).map(s => s.textContent)
    expect(subagentLines).toEqual([`Up to 4 at once, lowered from your${NBSP}8`, '3 running now'])
    const spawnGateLines = Array.from(spawnGate.querySelectorAll('span')).map(s => s.textContent)
    expect(spawnGateLines).toEqual([`Up to 2 at once, lowered from your${NBSP}8`])
    expect(screen.queryByText(/Below your limit/)).toBeNull()
    expect(screen.queryByText(/of \d+ slots/)).toBeNull()
    expect(screen.queryByText('Your limit: 8')).toBeNull()
    expect(screen.getByText('Subagent runs').closest('div')!.textContent).not.toContain('running:')
    // The lane is named by what it does, not by the mechanism behind it.
    expect(screen.getByText('Backend starts')).toBeTruthy()
    expect(screen.queryByText('Spawn gate')).toBeNull()
    expect(screen.getByText('loop_lag=410ms')).toBeTruthy()
    // Recovery column: attempts across retrying rows, restarting slots, stalled.
    expect(screen.getByText('Retry attempts').closest('div')!.textContent).toContain('3')
    expect(screen.getByText('Backends restarting').closest('div')!.textContent).toContain('1')
    expect(screen.getByText('Stalled', COUNTER).closest('div')!.textContent).toContain('1')
    // Waits list: one row per task/slot/stall with its reason, age and state
    // badge; the running slot is NOT a wait.
    const list = screen.getByRole('list', { name: 'Waiting & retrying' })
    const items = list.querySelectorAll('li')
    expect(items).toHaveLength(6)
    expect(list.textContent).toContain('github:api rate limited')
    expect(list.textContent).toContain('sudo wants a password')
    expect(list.textContent).toContain('Waiting for dependency')
    expect(list.textContent).toContain('Waiting for input')
    expect(list.textContent).toContain('attempts: 3')
    expect(list.textContent).toContain(`next retry in ${fmtAge(90)}`)
    // The age column names its clock: "for 5s" beside "next retry in 1m 30s",
    // never two bare durations on one row.
    const ages = screen.getAllByTestId('tasks-capacity-wait-age')
    expect(ages).toHaveLength(6)
    expect(ages.map(a => a.textContent)).toContain(`${fmtAge(3_700)} so far`)
    const recRow = Array.from(items).find(li => li.textContent!.includes('rec-1'))!
    expect(recRow.textContent).toContain(`next retry in ${fmtAge(90)}`)
    expect(recRow.querySelector('[data-testid="tasks-capacity-wait-age"]')!.textContent).toBe(`${fmtAge(5)} so far`)
    for (const a of ages) expect(a.getAttribute('title')).toBe('Time spent in the state the badge names.')
    expect(list.textContent).not.toContain('web-ok')
    // The stall leads, then the longest wait: the fold shows the first 8 rows, so
    // age alone would let the one row the red badge names fall behind it.
    expect(items[0].textContent).toContain('web-s')
    expect(items[1].textContent).toContain('inp-1')
    expect(screen.queryByTestId('tasks-capacity-empty')).toBeNull()

    // A retry IN FLIGHT and a row PARKED until its next attempt are two states:
    // different words, different colours. Same "next retry in …" clock on both,
    // so the badge is the only thing telling them apart.
    const badgeOf = (li: Element) => li.querySelector('span.rounded-full')!
    const parkRow = Array.from(items).find(li => li.textContent!.includes('park-1'))!
    expect(badgeOf(recRow).textContent).toBe('Retrying now')
    expect(badgeOf(parkRow).textContent).toBe('Waiting to retry')
    expect(badgeOf(recRow).className).toContain('text-warn')
    expect(badgeOf(parkRow).className).not.toContain('text-warn')
    expect(badgeOf(recRow).className).not.toBe(badgeOf(parkRow).className)
    expect(list.textContent).not.toContain('Retry wait')

    // "Recovering" names no counter, no column and no list heading here: one
    // word spent on three of them belongs to none, and a reader comparing the
    // card's own lines reads one word as one concept. Every counter carries its
    // own, across the copy AND the tooltips, and each counts a different unit.
    const card = screen.getByTestId('tasks-capacity-card')
    const prose = [card.textContent ?? '',
      ...Array.from(card.querySelectorAll('button[title]')).map(b => b.getAttribute('title'))].join(' ')
    expect(prose).not.toMatch(/recovering/i)
    expect(screen.getByText('Tasks retrying')).toBeTruthy()
    expect(screen.getByText('Retry attempts')).toBeTruthy()
    expect(screen.getByText('Backends restarting')).toBeTruthy()

    // EVERY row names a session, so every row whose session has a chat window
    // carries a NAMED link to it — not only the ones blocked on the reader. A row
    // that names a session and offers no way to open it is the dead end. The id
    // stays plain text throughout, so colour alone never carries the affordance.
    const links = screen.getAllByTestId('tasks-capacity-wait-link')
    expect(links.every(a => a.textContent === 'Open chat')).toBe(true)
    expect(links.every(a => a.getAttribute('aria-label') === null)).toBe(true)
    const inpRow = Array.from(items).find(li => li.textContent!.includes('inp-1'))!
    expect(inpRow.querySelector('a')!.getAttribute('href')).toBe('/chat?sid=web-inp')
    // The guard is the ROUTE's, not the row's state: these two rows wait on the
    // SYSTEM rather than the reader, but that is not why they are unlinked — their
    // fixture session key is `web-a`, which is not a dashboard session and so has
    // no chat window at all. A link there would be a click that does nothing.
    // (The linked-when-it-CAN case is covered by `inp-1` above and, for a
    // system-waiting row, by the slot test below.)
    const depRow = Array.from(items).find(li => li.textContent!.includes('dep-1'))!
    expect(depRow.querySelector('a')).toBeNull()
    expect(recRow.querySelector('a')).toBeNull()
  })

  it('links a slot waiting for approval to its chat, and renders no link when the session has no chat window', async () => {
    tasksSummary.mockResolvedValue(summary({
      waiting: [
        // A cron session is real but has nowhere to navigate to: plain text, no dead link.
        row('inp-cron', 'waiting_input', { wait_reason: 'needs a password', age_secs: 10, session_key: 'cron_abc' }),
        row('inp-none', 'waiting_input', { wait_reason: 'needs a token', age_secs: 9, session_key: '' }),
      ],
      slots: [
        { key: 'chat-7', classification: 'waiting_permission', age_secs: 30, evidence: ['approval pending'] },
      ],
    }))
    renderWithProviders(<TasksCapacityCard />)
    const links = await screen.findAllByTestId('tasks-capacity-wait-link')
    expect(links).toHaveLength(1)
    expect(links[0].textContent).toBe('Open chat')
    expect(links[0].getAttribute('href')).toBe('/chat?sid=chat-7')
    const list = screen.getByRole('list', { name: 'Waiting & retrying' })
    expect(list.querySelectorAll('a')).toHaveLength(1)
    expect(list.textContent).toContain('chat-7')
    expect(list.textContent).toContain('inp-cron')
    expect(list.textContent).toContain('inp-none')
  })

  it('links a row waiting on the SYSTEM when its session has a chat window', async () => {
    // The link is not an "you must act" marker, it is "here is where this is".
    // A dependency wait and a retry are the system's to clear, and the reader
    // still opens them — to read the reason against the transcript that produced
    // it. These two states are most of this list, so a link gated on
    // human-blocked would leave the bulk of the card unreachable.
    tasksSummary.mockResolvedValue(summary({
      waiting: [
        row('dep-1', 'waiting_dependency', {
          wait_reason: 'github:api rate limited', age_secs: 20, session_key: 'dashboard:web-dep',
        }),
      ],
      recovering: {
        tasks: [row('rec-1', 'retry_wait', { age_secs: 5, session_key: 'dashboard:web-rec' })],
        task_attempts: 1, slots: [], ladder: [],
      },
    }))
    renderWithProviders(<TasksCapacityCard />)
    const links = await screen.findAllByTestId('tasks-capacity-wait-link')
    const hrefs = links.map(a => a.getAttribute('href')).sort()
    expect(hrefs).toEqual(['/chat?sid=web-dep', '/chat?sid=web-rec'])
    expect(links.every(a => a.textContent === 'Open chat')).toBe(true)
  })

  it('names the action on a human-blocked row instead of only colouring its id', async () => {
    tasksSummary.mockResolvedValue(summary({
      waiting: [
        row('inp-1', 'waiting_input', { wait_reason: 'sudo wants a password', age_secs: 30, session_key: 'dashboard:web-inp' }),
        row('dep-1', 'waiting_dependency', { wait_reason: 'github:api rate limited', age_secs: 20 }),
      ],
    }))
    renderWithProviders(<TasksCapacityCard />)
    const link = await screen.findByTestId('tasks-capacity-wait-link')
    // The visible catalog text IS the accessible name: an aria-label that does
    // not contain it would break WCAG 2.5.3 in the locales that reorder words.
    expect(link.textContent).toBe('Open chat')
    expect(link.getAttribute('aria-label')).toBeNull()
    expect(link.getAttribute('title')).toBeNull()
    expect(link.getAttribute('href')).toBe('/chat?sid=web-inp')
    // No row renders its id as the control, actionable or not.
    for (const li of screen.getAllByTestId('tasks-capacity-wait-row')) {
      const anchor = li.querySelector('a')
      if (anchor) expect(anchor.textContent).toBe('Open chat')
    }
    expect(screen.queryByRole('link', { name: 'inp-1' })).toBeNull()
  })

  it('reads the degrade reason out of the catalog and passes an unknown one through', async () => {
    // No lane is under a ceiling in these payloads (`lanes: {}`), so the reason
    // has no number to sit beside and the row is where it belongs.
    const copy: Record<string, string> = {
      adaptive_decrease: 'Concurrency lowered under load',
      adaptive_pause: 'New runs paused until the host recovers',
      adaptive_probe: 'Testing one run before resuming',
    }
    for (const [token, text] of Object.entries(copy)) {
      tasksSummary.mockReset()
      tasksSummary.mockResolvedValue(summary({ degrade_reason: token }))
      const { unmount } = renderWithProviders(<TasksCapacityCard />)
      await waitFor(() =>
        expect(screen.getByTestId('tasks-capacity-degrade').textContent).toBe(text))
      // The badge says "Degraded" in the reader's language, so the explanation
      // beside it must not be the snake_case wire token, and a sentence is set in
      // the UI font rather than the column's mono.
      expect(screen.getByTestId('tasks-capacity-health').textContent).toBe('Degraded')
      expect(screen.queryByText(token)).toBeNull()
      expect(screen.getByTestId('tasks-capacity-degrade').className).toContain('font-body')
      unmount()
    }

    // The controller's own free-text reading is machine text: verbatim, mono,
    // and marked untranslatable rather than blanked.
    tasksSummary.mockReset()
    tasksSummary.mockResolvedValue(summary({ degrade_reason: 'paused: memory below critical' }))
    renderWithProviders(<TasksCapacityCard />)
    await waitFor(() => expect(screen.getByTestId('tasks-capacity-degrade').textContent)
      .toBe('paused: memory below critical'))
    expect(screen.getByTestId('tasks-capacity-degrade').getAttribute('translate')).toBe('no')
    expect(screen.getByTestId('tasks-capacity-degrade').className).not.toContain('font-body')
  })

  it('says why a lane is below the limit, and shows one number when it is not', async () => {
    tasksSummary.mockResolvedValue(summary({
      lanes: {
        subagents: { effective: 2, user_max: 14, running: 2 },
        spawn_gate: { effective: 8, user_max: 8, running: 0 },
        system: { effective: 3, user_max: null },
      },
      degrade_reason: 'adaptive_decrease',
    }))
    renderWithProviders(<TasksCapacityCard />)
    const [subagents, spawnGate, system] = await screen.findAllByTestId('tasks-capacity-lane')
    const linesOf = (el: Element) => Array.from(el.querySelectorAll('span')).map(s => s.textContent)
    // Lowered: ONE line carrying both numbers and the relationship between them,
    // then the cause in the same words the degrade row uses — one cause with two
    // wordings reads as two. Neither number is ever left standing alone as a
    // second, rival limit.
    expect(linesOf(subagents)).toEqual([
      `Up to 2 at once, lowered from your${NBSP}14`, 'Concurrency lowered under load', '2 running now',
    ])
    // Equal: ONE number. A second line repeating 8 presents two facts where the
    // reader has one, which is the pair no reader can relate ("Up to 4 at once"
    // against a bare "Your limit: 14").
    expect(linesOf(spawnGate)).toEqual(['Up to 8 at once', 'Your full limit', '0 running now'])
    // The cause has ONE home, the closest to the number it explains: the lane
    // lines carry it here, so the row would be a third printing of one sentence.
    expect(screen.queryByTestId('tasks-capacity-degrade')).toBeNull()
    expect(screen.queryByText('Degrade reason')).toBeNull()
    // No ceiling on the wire: nothing to relate the cap to, so no line claims one.
    expect(linesOf(system)).toEqual(['Up to 3 at once'])
    // Every line that says more than the bare cap is a SENTENCE about a number:
    // the UI font, and free to wrap. A column is a third of the card, so an
    // unbreakable phrase this long lands on top of the column beside it — which is
    // why the lowered lane, whose lead line carries both numbers, has no
    // unbreakable line at all.
    for (const line of Array.from(subagents.querySelectorAll('span'))) {
      expect(line.className).not.toContain('whitespace-nowrap')
      expect(line.className).toContain('font-body')
    }
    // The cap in force is never muted, on either shape of lead line: it is the
    // answer this column exists for, and the cause and the live count sit under it.
    const leadOf = (el: Element) => el.querySelector('span')!
    expect(leadOf(subagents).className).not.toContain('text-muted')
    expect(leadOf(spawnGate).className).not.toContain('text-muted')
    // A lane AT its ceiling keeps the short unbreakable headline: it is the bare
    // number, so it cannot overflow the column and must not wrap mid-phrase.
    const atLimitLines = Array.from(spawnGate.querySelectorAll('span'))
    expect(atLimitLines[0].className).toContain('whitespace-nowrap')
    for (const line of atLimitLines.slice(1)) {
      expect(line.className).not.toContain('whitespace-nowrap')
      expect(line.className).toContain('font-body')
    }
  })

  it('prints one cause once when TWO lanes are lowered, in the row and on neither lane', async () => {
    // `degrade_reason` is ONE global string and the controller lowers global
    // concurrency, so the lowered-lane count is normally two. A cause printed per
    // lowered lane is then the same sentence twice, which reads as two causes —
    // the defect the single-lane case cannot expose.
    tasksSummary.mockResolvedValue(summary({
      lanes: {
        subagents: { effective: 2, user_max: 14, running: 2 },
        spawn_gate: { effective: 2, user_max: 8 },
      },
      degrade_reason: 'adaptive_decrease',
    }))
    renderWithProviders(<TasksCapacityCard />)
    await screen.findAllByTestId('tasks-capacity-lane')
    // Exactly one printing of the sentence, anywhere on the card.
    expect(screen.getAllByText('Concurrency lowered under load')).toHaveLength(1)
    // ... and it is the row, because no single number on the card owns a cause
    // that lowered both lanes.
    expect(screen.getByTestId('tasks-capacity-degrade').textContent)
      .toBe('Concurrency lowered under load')
    const [subagents, spawnGate] = screen.getAllByTestId('tasks-capacity-lane')
    const linesOf = (el: Element) => Array.from(el.querySelectorAll('span')).map(s => s.textContent)
    expect(linesOf(subagents)).toEqual([`Up to 2 at once, lowered from your${NBSP}14`, '2 running now'])
    expect(linesOf(spawnGate)).toEqual([`Up to 2 at once, lowered from your${NBSP}8`])
  })

  it('falls back to the bare ceiling when the wire reports a cap above it', async () => {
    // Neither "your full limit" nor "below your limit" is true of a cap ABOVE the
    // configured one, so the card states the ceiling and claims no relationship.
    tasksSummary.mockResolvedValue(summary({ lanes: { subagents: { effective: 6, user_max: 4 } } }))
    renderWithProviders(<TasksCapacityCard />)
    const lane = await screen.findByTestId('tasks-capacity-lane')
    expect(Array.from(lane.querySelectorAll('span')).map(s => s.textContent))
      .toEqual(['Up to 6 at once', 'Your limit: 4'])
  })

  it('renders no degrade row when nothing lowered the cap, stalled badge included', async () => {
    tasksSummary.mockResolvedValue(summary({
      lanes: { subagents: { effective: 4, user_max: 4, running: 0 } },
      stalled: { 'web-s': { reason: 'no_progress', since_ts: 0, age_secs: 700, evidence: [] } },
    }))
    renderWithProviders(<TasksCapacityCard />)
    expect((await screen.findByTestId('tasks-capacity-health')).textContent).toBe('Stalled')
    // A labelled "none" beside a red badge reads as a missing answer rather than
    // as "nothing degraded this". The row is the reason's home, so it exists
    // exactly when a reason does — and the stalled COUNT is where a stall is told.
    expect(screen.queryByTestId('tasks-capacity-degrade')).toBeNull()
    expect(screen.queryByText('Degrade reason')).toBeNull()
    expect(screen.getByTestId('tasks-capacity-card').textContent).not.toMatch(/\bnone\b/i)
    expect(screen.getByText('Stalled', COUNTER).closest('div')!.textContent).toContain('1')
  })

  it('gives the stalled count a row to open, with the stall evidence on it', async () => {
    // `stalled` is its own wire field: the monitor fills it for a slot it has
    // classified, and the panel's summary carries it whether or not a `slots`
    // entry mirrors it. A red badge and "Stalled: 1" with nothing in the list is
    // an alarm with no drill-in, at the one moment a reader needs the row.
    tasksSummary.mockResolvedValue(summary({
      stalled: {
        'chat-7': {
          reason: 'no_progress', since_ts: 0, age_secs: 11 * 60,
          evidence: ['shell child absent', 'no cpu delta'],
        },
      },
      waiting: [row('dep-1', 'waiting_dependency', { wait_reason: 'github:api rate limited', age_secs: 20 })],
    }))
    renderWithProviders(<TasksCapacityCard />)
    const list = await screen.findByRole('list', { name: 'Waiting & retrying' })
    const items = list.querySelectorAll('li')
    expect(items).toHaveLength(2)
    const stalledRow = Array.from(items).find(li => li.textContent!.includes('chat-7'))!
    // The badge names the state, the reason column carries the EVIDENCE — the wire
    // `reason` reads `no_progress` for every structured stall, which is the badge
    // again rather than a second fact.
    expect(stalledRow.querySelector('span.rounded-full')!.textContent).toBe('Stalled')
    expect(stalledRow.querySelector('span.rounded-full')!.className).toContain('text-danger')
    expect(stalledRow.querySelector('[data-testid="tasks-capacity-wait-reason"]')!.textContent)
      .toBe('shell child absent · no cpu delta')
    expect(stalledRow.textContent).not.toContain('no_progress')
    expect(stalledRow.querySelector('[data-testid="tasks-capacity-wait-age"]')!.textContent)
      .toBe(`${fmtAge(11 * 60)} so far`)
    // The stall is the row with NO self-recovery, so it is the one that most needs
    // an exit: it links to its own slot's chat, where the evidence above can be
    // read against the transcript that produced it. The card stays read-only —
    // this navigates and nothing more.
    const stalledLink = stalledRow.querySelector('a')!
    expect(stalledLink.textContent).toBe('Open chat')
    expect(stalledLink.getAttribute('href')).toBe('/chat?sid=chat-7')
    // The counter and the row are read off one array, so they cannot disagree.
    expect(screen.getByText('Stalled', COUNTER).closest('div')!.textContent).toContain('1')
  })

  it('renders ONE row for a stall the slot list also reports', async () => {
    // The monitor writes both fields from the same branch, so the ordinary payload
    // carries the stall twice; two rows for one stall would make the list
    // disagree with the count that points at it.
    tasksSummary.mockResolvedValue(summary({
      stalled: { 'chat-7': { reason: 'no_progress', since_ts: 0, age_secs: 700, evidence: ['no cpu delta'] } },
      slots: [{ key: 'chat-7', classification: 'stalled', age_secs: 700, evidence: ['no cpu delta'] }],
    }))
    const { unmount } = renderWithProviders(<TasksCapacityCard />)
    const list = await screen.findByRole('list', { name: 'Waiting & retrying' })
    expect(list.querySelectorAll('li')).toHaveLength(1)
    expect(screen.getByText('Stalled', COUNTER).closest('div')!.textContent).toContain('1')
    unmount()

    // And a slot the monitor classified stalled with no `stalled` entry beside it
    // is still one row and one count — the badge, the counter and the list all read
    // the same set.
    tasksSummary.mockReset()
    tasksSummary.mockResolvedValue(summary({
      slots: [{ key: 'chat-9', classification: 'stalled', age_secs: 42, evidence: ['no tool output'] }],
    }))
    renderWithProviders(<TasksCapacityCard />)
    const fresh = await screen.findByRole('list', { name: 'Waiting & retrying' })
    expect(fresh.querySelectorAll('li')).toHaveLength(1)
    expect(fresh.textContent).toContain('chat-9')
    expect(screen.getByTestId('tasks-capacity-health').textContent).toBe('Stalled')
    expect(screen.getByText('Stalled', COUNTER).closest('div')!.textContent).toContain('1')
  })

  it('keeps the stalled row in the bounded fold, however young the stall is', async () => {
    // Eleven long waits and one 5-second stall: ordered by age alone the row the
    // red badge names is the one row the fold hides.
    tasksSummary.mockResolvedValue(summary({
      waiting: Array.from({ length: 11 }, (_, i) =>
        row(`w-${i}`, 'waiting_children', { age_secs: 3_600 - i })),
      stalled: { 'chat-7': { reason: 'no_progress', since_ts: 0, age_secs: 5, evidence: ['no cpu delta'] } },
    }))
    renderWithProviders(<TasksCapacityCard />)
    const list = await screen.findByRole('list', { name: 'Waiting & retrying' })
    const items = list.querySelectorAll('li')
    expect(items).toHaveLength(8)
    expect(items[0].textContent).toContain('chat-7')
    // The fold is still bounded and still opens the rest in place.
    expect(screen.getByTestId('tasks-capacity-fold').textContent).toBe('Show 4 more')
  })

  it('counts a row parked until its next attempt under retries only, never as a slot waiter', async () => {
    // `depth.queued` sums queued + admitted + retry_wait + waiting_infra, and
    // `depth.recovering` counts those same retry_wait rows again: the wire hands
    // the card one row under two counters, which is the pair a reader cannot
    // reconcile without a tooltip open.
    tasksSummary.mockResolvedValue(summary({
      depth: {
        by_state: { queued: 9, admitted: 2, waiting_infra: 1, retry_wait: 2, running: 1 },
        queued: 14, waiting: 0, recovering: 2, running: 1, total: 15,
      },
      recovering: {
        tasks: [
          row('park-1', 'retry_wait', { attempts: 2, age_secs: 4, next_run_at: 1_030 }),
          row('park-2', 'retry_wait', { attempts: 1, age_secs: 3, next_run_at: 1_040 }),
        ],
        task_attempts: 3,
        slots: [],
        ladder: [],
      },
    }))
    renderWithProviders(<TasksCapacityCard />)
    await screen.findByRole('list', { name: 'Waiting & retrying' })
    // 14 accepted − the 2 rows waiting to retry, which "Tasks retrying" counts.
    const slotWaiters = screen.getByText('Waiting for a slot').closest('div')!
    expect(slotWaiters.textContent).toContain('12')
    expect(slotWaiters.textContent).not.toContain('14')
    expect(screen.getByText('Tasks retrying').closest('div')!.textContent).toContain('2')
    // The tip states the disjointness rule, never an overlap: a reader comparing
    // the two counters at rest has no tooltip open.
    const tip = slotWaiters.querySelector('button')!.getAttribute('title')!
    expect(tip).toContain('Tasks retrying')
    expect(tip).toContain('no row is counted twice')
    expect(tip).not.toMatch(/also counted/i)
  })

  it('never reads Backlog off a queue count the card does not show', async () => {
    // 9 accepted with a cap of 4 is over 2× only while the 5 rows parked until
    // their next attempt are counted in: the badge stands on the number beside it,
    // so an amber "Backlog" over "Waiting for a slot: 4" would be a verdict the
    // card contradicts on the same line.
    const deep = summary({
      depth: {
        by_state: { queued: 4, retry_wait: 5 },
        queued: 9, waiting: 0, recovering: 5, running: 0, total: 9,
      },
      lanes: { subagents: { effective: 4, user_max: 4, running: 0 } },
    })
    expect(cardHealth(deep)).toEqual({ variant: 'ok', state: 'healthy' })
    tasksSummary.mockResolvedValue(deep)
    renderWithProviders(<TasksCapacityCard />)
    const badge = await screen.findByTestId('tasks-capacity-health')
    expect(badge.textContent).toBe('Healthy')
    expect(screen.getByText('Waiting for a slot').closest('div')!.textContent).toContain('4')
    // One more genuine slot waiter, and the same rule turns the badge amber.
    expect(cardHealth({
      ...deep,
      depth: { ...deep.depth, by_state: { queued: 9, retry_wait: 5 }, queued: 14 },
    })).toEqual({ variant: 'warn', state: 'backlog' })
  })

  it('counts a chat session paused on the operator, not only the task rows', async () => {
    // `depth.waiting` is the store's own number, so a live slot parked on an
    // approval used to be a list row that no counter on the card admitted to.
    tasksSummary.mockResolvedValue(summary({
      depth: { by_state: {}, queued: 0, waiting: 1, recovering: 0, running: 0, total: 1 },
      waiting: [row('inp-2', 'waiting_input', { wait_reason: 'needs a token', age_secs: 20, session_key: 'dashboard:web-inp' })],
      slots: [{ key: 'chat-7', classification: 'waiting_permission', age_secs: 30, evidence: ['approval pending'] }],
    }))
    renderWithProviders(<TasksCapacityCard />)
    const list = await screen.findByRole('list', { name: 'Waiting & retrying' })
    expect(list.querySelectorAll('li')).toHaveLength(2)
    const paused = screen.getByText('Paused mid-run').closest('div')!
    expect(paused.textContent).toContain('2')
    expect(paused.textContent).not.toContain('1')
  })

  it('names nested work with ONE word everywhere the card mentions it', async () => {
    // Never a second word for nested work: the badge, the lane and the tip name
    // it, and a card whose whole point is comparing its own lines reads two
    // words as two concepts. "Task" is taken here — it means a queue ROW
    // ("Tasks retrying", "Retry attempts"). The `wait_reason` is the backend's
    // untranslated evidence prose, in the same row as the badge, so the sweep
    // below covers it too rather than only the catalog.
    tasksSummary.mockResolvedValue(summary({
      depth: { by_state: {}, queued: 0, waiting: 1, recovering: 0, running: 1, total: 2 },
      lanes: { subagents: { effective: 4, user_max: 8, running: 1 } },
      waiting: [row('kid-1', 'waiting_children',
        { wait_reason: 'waiting on 2 subagent(s)', age_secs: 12 })],
    }))
    renderWithProviders(<TasksCapacityCard />)
    const list = await screen.findByRole('list', { name: 'Waiting & retrying' })
    expect(list.textContent).toContain('Waiting for subagents')
    expect(screen.getByText('Subagent runs')).toBeTruthy()
    const pausedTip = screen.getByText('Paused mid-run').closest('div')!.querySelector('button')!
    expect(pausedTip.getAttribute('title')).toContain('subagents')
    const card = screen.getByTestId('tasks-capacity-card')
    const prose = [card.textContent ?? '',
      ...Array.from(card.querySelectorAll('button[title]')).map(b => b.getAttribute('title'))].join(' ')
    expect(prose).not.toMatch(/child|subtask|nested/i)
  })

  it('opens the folded rows in place instead of naming a count nothing can reach', async () => {
    tasksSummary.mockResolvedValue(summary({
      waiting: Array.from({ length: 11 }, (_, i) => row(`w-${i}`, 'waiting_children', { age_secs: i })),
    }))
    renderWithProviders(<TasksCapacityCard />)
    const rows = () => screen.getByRole('list', { name: 'Waiting & retrying' }).querySelectorAll('li')
    // Bounded by default — the card must not grow with the queue ...
    const fold = await screen.findByTestId('tasks-capacity-fold')
    expect(fold.tagName).toBe('BUTTON')
    expect(fold.textContent).toBe('Show 3 more')
    expect(fold.getAttribute('aria-expanded')).toBe('false')
    expect(fold.getAttribute('aria-controls')).toBe('tasks-capacity-waits')
    expect(rows()).toHaveLength(8)

    // ... and the held-back rows are one click away, in the card, not behind a
    // line of text with nowhere to go.
    fireEvent.click(fold)
    expect(rows()).toHaveLength(11)
    expect(screen.getByTestId('tasks-capacity-fold').getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByTestId('tasks-capacity-fold').textContent).toBe('Show fewer')
    expect(screen.getByRole('list', { name: 'Waiting & retrying' }).textContent).toContain('w-10')

    fireEvent.click(screen.getByTestId('tasks-capacity-fold'))
    expect(rows()).toHaveLength(8)
    expect(screen.getByTestId('tasks-capacity-fold').textContent).toBe('Show 3 more')
  })

  it('renders no fold control when every row already fits', async () => {
    tasksSummary.mockResolvedValue(summary({
      waiting: Array.from({ length: 8 }, (_, i) => row(`w-${i}`, 'waiting_children', { age_secs: i })),
    }))
    renderWithProviders(<TasksCapacityCard />)
    await screen.findByRole('list', { name: 'Waiting & retrying' })
    expect(screen.queryByTestId('tasks-capacity-fold')).toBeNull()
  })

  it('derives the badge from the numbers the card shows, not from degrade_reason alone', async () => {
    // 14 queued, an oldest wait past the stall threshold and a stalled run
    // with NO degrade reason: the reviewer's case, which used to read Healthy.
    tasksSummary.mockResolvedValue(summary({
      depth: { by_state: {}, queued: 14, waiting: 4, recovering: 2, running: 2, total: 22 },
      oldest_wait_secs: 1_308,
      lanes: { subagents: { effective: 4, user_max: 14, running: 0 } },
      stalled: { 'web-s': { reason: 'no_progress', since_ts: 0, age_secs: 700, evidence: [] } },
    }))
    renderWithProviders(<TasksCapacityCard />)
    const badge = await screen.findByTestId('tasks-capacity-health')
    expect(badge.textContent).toBe('Stalled')
    expect(badge.className).toContain('text-danger')
    expect(screen.queryByText('Healthy')).toBeNull()
  })

  it('renders the backlog badge in amber when the queue is late but nothing is stalled', async () => {
    tasksSummary.mockResolvedValue(summary({
      depth: { by_state: {}, queued: 14, waiting: 0, recovering: 0, running: 2, total: 16 },
      oldest_wait_secs: 1_308,
      lanes: { subagents: { effective: 4, user_max: 14, running: 2 } },
    }))
    renderWithProviders(<TasksCapacityCard />)
    const badge = await screen.findByTestId('tasks-capacity-health')
    // "Backed up" collides with a backup copy on first reading; the queue state
    // is a backlog.
    expect(badge.textContent).toBe('Backlog')
    expect(badge.className).toContain('text-warn')
    expect(screen.queryByText(/Backed up/)).toBeNull()
  })

  it('keeps a long reason un-truncated at the narrow layout and only clips from sm up', async () => {
    const reason = 'GitHub API rate limited for repo kirodotdev/KiroCrew; the coordinator resumes this run once the reset window at the top of the hour has passed'
    tasksSummary.mockResolvedValue(summary({
      waiting: [row('dep-long', 'waiting_dependency', { wait_reason: reason, age_secs: 70, attempts: 3 })],
    }))
    renderWithProviders(<TasksCapacityCard />)
    const el = await screen.findByTestId('tasks-capacity-wait-reason')
    // The full sentence is in the DOM (not elided) ...
    expect(el.textContent).toBe(reason)
    // ... and truncation is a `sm:`-scoped utility only: at narrow widths the
    // span wraps (`break-words`) instead of relying on a hover-only title.
    const classes = el.className.split(/\s+/)
    expect(classes).toContain('break-words')
    expect(classes).toContain('sm:truncate')
    expect(classes).not.toContain('truncate')
    // The row stacks narrow-first and becomes a single row from sm up.
    const li = screen.getByTestId('tasks-capacity-wait-row')
    expect(li.className.split(/\s+/)).toEqual(expect.arrayContaining(['flex-col', 'sm:flex-row']))
  })

  // The notice's own text, without the agent hand-off button beside it.
  const noticeText = () =>
    screen.getByTestId('tasks-capacity-error').querySelector('div')!.textContent

  it('renders the failure through ErrorNotice, not a bare div', async () => {
    tasksSummary.mockRejectedValue(new Error('503 task store unavailable'))
    renderWithProviders(<TasksCapacityCard />)
    await waitFor(() => expect(screen.getByTestId('tasks-capacity-error')).toBeTruthy())
    // A reason that IS a sentence stays: it is the half of the notice that says
    // what happened.
    expect(noticeText()).toBe('Could not load the task queue 503 task store unavailable')
  })

  it('shows the failure sentence alone when the reason is not prose', async () => {
    // A refusal whose body is an empty JSON envelope arrives as `{}`; printed
    // after the sentence it reads as a blank where the cause belongs. Every
    // spelling of "no reason given" is suppressed, not just that one.
    for (const raw of ['{}', '[]', '  ', 'HTTP 500', '500', '<!DOCTYPE html><html>']) {
      tasksSummary.mockReset()
      tasksSummary.mockRejectedValue(new Error(raw))
      const { unmount } = renderWithProviders(<TasksCapacityCard />)
      await waitFor(() => expect(screen.getByTestId('tasks-capacity-error')).toBeTruthy())
      expect(noticeText()).toBe('Could not load the task queue')
      // The hand-off survives the suppression: the agent still gets the failure.
      expect(screen.getByTestId('tasks-capacity-error').textContent).toContain('Ask the agent')
      unmount()
    }
  })
})

describe('proseReason', () => {
  it('keeps a sentence and drops every shape that carries no reason', () => {
    expect(proseReason('503 task store unavailable')).toBe('503 task store unavailable')
    expect(proseReason('Failed to fetch')).toBe('Failed to fetch')
    expect(proseReason('  paused: memory below critical  ')).toBe('paused: memory below critical')
    // Serialized envelopes, whatever they contain.
    expect(proseReason('{}')).toBeNull()
    expect(proseReason('{"code": "unavailable"}')).toBeNull()
    expect(proseReason('[]')).toBeNull()
    expect(proseReason('<!DOCTYPE html><html>')).toBeNull()
    // One letter-bearing word or none is a status token, not a reason.
    expect(proseReason('HTTP 500')).toBeNull()
    expect(proseReason('500')).toBeNull()
    expect(proseReason('')).toBeNull()
    expect(proseReason('   ')).toBeNull()
  })
})

describe('cardHealth', () => {
  const base = () => summary({
    depth: { by_state: {}, queued: 0, waiting: 0, recovering: 0, running: 0, total: 0 },
    lanes: { subagents: { effective: 4, user_max: 8, running: 0 } },
    stall_after_secs: 600,
  })

  it('is healthy on an empty queue with nothing degraded', () => {
    expect(cardHealth(base())).toEqual({ variant: 'ok', state: 'healthy' })
  })

  it('states no verdict without a store, unless a stall or the controller supports one', () => {
    // The healthy and backlog rules read `depth` and `oldest_wait_secs`, which a
    // store-less gateway can only send as zeros: "healthy" there is a claim about
    // data that does not exist. `stalled` (the live session monitor) and
    // `degrade_reason` (the controller) are published without a store, so those two
    // verdicts still stand — and one of them going missing would cost the reader
    // the only alarm such an install can raise.
    const noStore = summary({ ...base(), available: false })
    expect(cardHealth(noStore)).toBeNull()
    expect(cardHealth({ ...noStore, oldest_wait_secs: 5_000 })).toBeNull()
    expect(cardHealth({ ...noStore, degrade_reason: 'adaptive_pause' }))
      .toEqual({ variant: 'warn', state: 'degraded' })
    expect(cardHealth({
      ...noStore,
      stalled: { s: { reason: 'no_progress', since_ts: 0, age_secs: 1, evidence: [] } },
    })).toEqual({ variant: 'err', state: 'stalled' })
  })

  it('goes red for a slot the monitor classified stalled, not only for a stalled map entry', () => {
    // Badge, Recovery counter and list rows read ONE set, so a stall can never
    // colour the card without a row to open, or the reverse.
    expect(cardHealth({
      ...base(),
      slots: [{ key: 'chat-7', classification: 'stalled', age_secs: 30, evidence: [] }],
    })).toEqual({ variant: 'err', state: 'stalled' })
  })

  it('ranks stalled above degraded above backlog', () => {
    const all = summary({
      ...base(),
      degrade_reason: 'loop_lag=410ms',
      oldest_wait_secs: 5_000,
      depth: { by_state: {}, queued: 40, waiting: 0, recovering: 0, running: 0, total: 40 },
      stalled: { s: { reason: 'no_progress', since_ts: 0, age_secs: 1, evidence: [] } },
    })
    expect(cardHealth(all)).toEqual({ variant: 'err', state: 'stalled' })
    expect(cardHealth({ ...all, stalled: {} })).toEqual({ variant: 'warn', state: 'degraded' })
    expect(cardHealth({ ...all, stalled: {}, degrade_reason: null })).toEqual({ variant: 'warn', state: 'backlog' })
  })

  it('calls the queue backed up at the stall threshold, and one second under it healthy', () => {
    expect(cardHealth({ ...base(), oldest_wait_secs: 600 }).state).toBe('backlog')
    expect(cardHealth({ ...base(), oldest_wait_secs: 599 }).state).toBe('healthy')
    // No published threshold → the 10-minute fallback.
    expect(cardHealth({ ...base(), stall_after_secs: null, oldest_wait_secs: 600 }).state).toBe('backlog')
    expect(cardHealth({ ...base(), stall_after_secs: null, oldest_wait_secs: 599 }).state).toBe('healthy')
  })

  it('calls the queue backed up past 2× the widest effective lane cap, and never without a cap', () => {
    const depth = (queued: number) => ({ by_state: {}, queued, waiting: 0, recovering: 0, running: 0, total: queued })
    expect(cardHealth({ ...base(), depth: depth(9) }).state).toBe('backlog')
    expect(cardHealth({ ...base(), depth: depth(8) }).state).toBe('healthy')
    // The widest lane sets the bar, not the narrowest.
    const lanes = { subagents: { effective: 4, user_max: 8 }, spawn_gate: { effective: 2, user_max: 8 } }
    expect(cardHealth({ ...base(), lanes, depth: depth(8) }).state).toBe('healthy')
    // No lane reports an effective cap: depth alone cannot say backlog.
    expect(cardHealth({ ...base(), lanes: {}, depth: depth(500) }).state).toBe('healthy')
  })
})

describe('fmtAge', () => {
  it('keeps seconds under an hour, drops them past it, and dashes bad input', () => {
    expect(fmtAge(42)).toContain('42')
    expect(fmtAge(3_700)).not.toContain('40')   // 1h 1m 40s → seconds dropped
    expect(fmtAge(3_700)).toContain('1')
    expect(fmtAge(-1)).toBe('—')
    expect(fmtAge(null)).toBe('—')
  })
})
