import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

/**
 * The crewmate's Perpetual mode switch on its detail page.
 *
 * What is pinned: the switch's position is the registry READ, never the press
 * (a refused press leaves it where the backend is); pending disables rather
 * than flips; each verdict renders its plain-words reason, including the
 * crewmate's own stop words; a thread never opened shows the switch disabled
 * with the reason instead of pressing into a 409; and a failed read never
 * renders as "nothing wakes this crewmate".
 */

const H = vi.hoisted(() => ({
  members: vi.fn(),
  autonudgeList: vi.fn(),
  memberPerpetualSet: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  api: {
    members: H.members,
    autonudgeList: H.autonudgeList,
    memberPerpetualSet: H.memberPerpetualSet,
  },
}))

import CrewPerpetualSection from './CrewPerpetualSection'

function wrap(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>)
}

const ROW = { name: 'Radar', slug: 'radar', slot_key: 'member-radar', running: false }
const now = Math.floor(Date.now() / 1000)
const LOOP = {
  id: 'lp1', slot_key: 'member-radar', message: 'Perpetual mode wake.', banner: 'Perpetual mode',
  idle_secs: 3600, max_cycles: 0, max_runtime_secs: 0, cycle_count: 7, active: true,
  created_ts: now - 30_000, last_fire_ts: now - 600, next_due_ts: now + 3000, gate: false,
}

beforeEach(() => {
  H.members.mockReset(); H.autonudgeList.mockReset(); H.memberPerpetualSet.mockReset()
  H.members.mockResolvedValue({ members: [ROW] })
})
afterEach(cleanup)

const switchEl = () => screen.getByTestId('crew-perpetual-switch').querySelector('[role="switch"]') as HTMLElement

describe('CrewPerpetualSection', () => {
  it('reads ON from the registry and shows the wake readouts', async () => {
    H.autonudgeList.mockResolvedValue({ enabled: true, loops: [LOOP] })
    wrap(<CrewPerpetualSection crew="Radar" />)
    await waitFor(() => expect(switchEl().getAttribute('aria-checked')).toBe('true'))
    expect(screen.getByTestId('crew-perpetual-status').getAttribute('data-state')).toBe('on')
    const interval = screen.getByTestId('crew-perpetual-interval')
    expect(interval.textContent).toMatch(/1\s?h/)
    expect(interval.parentElement?.className).toContain('flex-col')
    expect(interval.parentElement?.className).toContain('sm:flex-row')
    expect(interval.previousElementSibling?.className).toContain('sm:w-24')
    const title = screen.getByRole('heading', { name: /Perpetual mode/i })
    expect(title.className).toContain('min-w-0')
    expect(title.parentElement?.className).toContain('flex-wrap')
    expect(screen.getByTestId('crew-perpetual-cycles').textContent).toMatch(/7/)
    expect(screen.getByTestId('crew-perpetual-next').textContent).toMatch(/Due in/)
    // The one-line hint is the switch's own accessible description.
    expect(switchEl().getAttribute('aria-describedby')).toBe('crew-perpetual-hint')
    const hint = screen.getByTestId('crew-perpetual-hint').textContent ?? ''
    expect(hint).toMatch(/no cycle or time limit.*flipped back anytime/)
    // Both positions are stated: OFF is plainly "no wake on its own until
    // turned on again", not left to be inferred from the ON sentence.
    expect(hint).toMatch(/When off, it does not wake on its own until you turn it on again\./)
  })

  it('reads OFF with the coded reason and the crewmate\'s own words', async () => {
    H.autonudgeList.mockResolvedValue({
      enabled: true,
      loops: [{ ...LOOP, active: false, stopped_reason: 'autonudge_stop', stopped_detail: 'standing duty is over' }],
    })
    wrap(<CrewPerpetualSection crew="Radar" />)
    await waitFor(() => expect(switchEl().getAttribute('aria-checked')).toBe('false'))
    expect(screen.getByTestId('crew-perpetual-status').getAttribute('data-state')).toBe('off')
    expect(screen.getByTestId('crew-perpetual-reason').textContent).toMatch(/Stopped by the crewmate itself/)
    expect(screen.getByTestId('crew-perpetual-detail').textContent).toBe('standing duty is over')
  })

  it('reads OFF as "turned off by you" for the owner\'s manual pause', async () => {
    H.autonudgeList.mockResolvedValue({ enabled: true, loops: [{ ...LOOP, active: false, stopped_reason: 'manual' }] })
    wrap(<CrewPerpetualSection crew="Radar" />)
    expect((await screen.findByTestId('crew-perpetual-reason')).textContent).toMatch(/Turned off by you/)
    expect(screen.queryByTestId('crew-perpetual-detail')).toBeNull()
  })

  it('reads "nothing wakes it" when no loop was ever armed, switch OFF', async () => {
    H.autonudgeList.mockResolvedValue({ enabled: true, loops: [] })
    wrap(<CrewPerpetualSection crew="Radar" />)
    expect((await screen.findByTestId('crew-perpetual-reason')).textContent).toMatch(/never been turned on/)
    expect(switchEl().getAttribute('aria-checked')).toBe('false')
    expect(switchEl().getAttribute('aria-disabled')).toBeNull()
  })

  it('a press asks the server, disables while pending, then re-reads the registry', async () => {
    let answer: (v: unknown) => void = () => {}
    H.memberPerpetualSet.mockImplementation(() => new Promise((res) => { answer = res }))
    H.autonudgeList.mockResolvedValueOnce({ enabled: true, loops: [] })
    wrap(<CrewPerpetualSection crew="Radar" />)
    await screen.findByTestId('crew-perpetual-reason')
    fireEvent.click(switchEl())
    // The mutation runs its function on the next tick, so the call is awaited.
    await waitFor(() => expect(H.memberPerpetualSet).toHaveBeenCalledWith('radar', 'Radar', true))
    // Pending: disabled and announced, NOT flipped -- the switch holds no truth of its own.
    await waitFor(() => expect(screen.getByTestId('crew-perpetual-control').getAttribute('aria-busy')).toBe('true'))
    expect(switchEl().getAttribute('aria-checked')).toBe('false')
    expect(switchEl().getAttribute('aria-disabled')).toBe('true')
    expect(screen.getByText(/^Saving/)).toBeTruthy()
    // The answer lands; the registry now holds the armed loop and the switch flips from THAT read.
    H.autonudgeList.mockResolvedValue({ enabled: true, loops: [LOOP] })
    answer({ ok: true, loop: LOOP })
    await waitFor(() => expect(switchEl().getAttribute('aria-checked')).toBe('true'))
    expect(screen.getByTestId('crew-perpetual-control').getAttribute('aria-busy')).toBeNull()
    // Success is STATED where "Saving…" was, briefly: the press happens beside a
    // Save footer that stays disabled, so the card changing is not the only cue.
    expect(screen.getByTestId('crew-perpetual-save-state').textContent).toBe('Saved')
    await waitFor(() => expect(screen.queryByTestId('crew-perpetual-save-state')).toBeNull(), { timeout: 4000 })
  })

  it('a refused press shows the plain-words reason and the switch stays where the backend is', async () => {
    H.autonudgeList.mockResolvedValue({ enabled: true, loops: [] })
    const err = Object.assign(new Error('this member is running a structured monitor; stop it from its own thread'), {
      status: 409,
      body: JSON.stringify({ error: 'x', code: 'structured_monitor_not_convertible' }),
    })
    H.memberPerpetualSet.mockRejectedValue(err)
    wrap(<CrewPerpetualSection crew="Radar" />)
    await screen.findByTestId('crew-perpetual-reason')
    fireEvent.click(switchEl())
    expect((await screen.findByTestId('crew-perpetual-error')).textContent).toMatch(/watch task it set up in its chat.*stop the task there first/)
    expect(switchEl().getAttribute('aria-checked')).toBe('false')
    expect(switchEl().getAttribute('aria-disabled')).toBeNull()
    // No hand-off by default: the host's schedules pane may hold a draft this
    // section cannot see, and the hand-off would unmount it.
    expect(screen.queryByText('Ask the agent')).toBeNull()
  })

  it('offers the same "Ask the agent" hand-off as the side panel when the host says nothing is at stake', async () => {
    H.autonudgeList.mockResolvedValue({ enabled: true, loops: [] })
    H.memberPerpetualSet.mockRejectedValue(
      Object.assign(new Error('x'), { status: 409, body: JSON.stringify({ error: 'x', code: 'structured_monitor_not_convertible' }) }),
    )
    wrap(<CrewPerpetualSection crew="Radar" askAgent />)
    await screen.findByTestId('crew-perpetual-reason')
    fireEvent.click(switchEl())
    const notice = await screen.findByTestId('crew-perpetual-error')
    expect(notice.querySelector('button')?.textContent).toBe('Ask the agent')
  })

  it('an uncoded refusal shows the server\'s own sentence', async () => {
    H.autonudgeList.mockResolvedValue({ enabled: true, loops: [LOOP] })
    H.memberPerpetualSet.mockRejectedValue(Object.assign(new Error('trust record unreadable'), { status: 503, body: '' }))
    wrap(<CrewPerpetualSection crew="Radar" />)
    await waitFor(() => expect(switchEl().getAttribute('aria-checked')).toBe('true'))
    fireEvent.click(switchEl())
    expect((await screen.findByTestId('crew-perpetual-error')).textContent).toMatch(/trust record unreadable/)
    expect(H.memberPerpetualSet).toHaveBeenCalledWith('radar', 'Radar', false)
  })

  it('a thread never opened shows the switch disabled with the reason, and never presses', async () => {
    H.members.mockResolvedValue({ members: [{ ...ROW, slot_key: '' }] })
    H.autonudgeList.mockResolvedValue({ enabled: true, loops: [] })
    wrap(<CrewPerpetualSection crew="Radar" />)
    await waitFor(() => expect(switchEl().getAttribute('aria-disabled')).toBe('true'))
    expect(screen.getByTestId('crew-perpetual-thread-closed').textContent).toMatch(/runs in this crewmate's chat.*Open its chat once first/)
    expect(switchEl().getAttribute('aria-describedby')).toContain('crew-perpetual-thread-closed')
    fireEvent.click(switchEl())
    await new Promise((r) => setTimeout(r, 20))
    expect(H.memberPerpetualSet).not.toHaveBeenCalled()
  })

  it('a structured monitor on the thread reads none: the roster\'s word wins over the reduced registry row', async () => {
    // `/api/autonudge` publishes a monitor as a reduced row with `active: true`
    // and no cycle accounting; the roster's `perpetual` is computed with
    // `is_structured_monitor_loop` applied, so the switch reads OFF / never
    // turned on and offers nothing the route would refuse with 409.
    H.members.mockResolvedValue({ members: [{ ...ROW, perpetual: 'none' }] })
    H.autonudgeList.mockResolvedValue({
      enabled: true,
      loops: [{ id: 'mon1', slot_key: 'member-radar', active: true, idle_secs: 300, next_due_ts: now + 200, gate: false }],
    })
    wrap(<CrewPerpetualSection crew="Radar" />)
    await waitFor(() => expect(switchEl().getAttribute('aria-checked')).toBe('false'))
    expect(screen.getByTestId('crew-perpetual-status').getAttribute('data-state')).toBe('none')
    // Named for what it is, not "never turned on": the reader sees why the
    // switch is off. State only -- the remedy (open the chat, stop the task)
    // is the refusal's, said once when the switch is pressed, not repeated here.
    const reason = screen.getByTestId('crew-perpetual-reason').textContent ?? ''
    expect(reason).toMatch(/watch task it set up in its chat is running instead\./)
    expect(reason).not.toMatch(/stop the task/i)
    expect(reason).not.toMatch(/open its chat/i)
    expect(screen.queryByTestId('crew-perpetual-next')).toBeNull()
  })

  it('the roster\'s perpetual field decides the state when it disagrees with the record', async () => {
    H.members.mockResolvedValue({ members: [{ ...ROW, perpetual: 'off' }] })
    H.autonudgeList.mockResolvedValue({ enabled: true, loops: [LOOP] })
    wrap(<CrewPerpetualSection crew="Radar" />)
    await waitFor(() => expect(switchEl().getAttribute('aria-checked')).toBe('false'))
    expect(screen.getByTestId('crew-perpetual-status').getAttribute('data-state')).toBe('off')
  })

  it('withholds the switch and never says "nothing wakes it" when the registry read failed', async () => {
    H.autonudgeList.mockRejectedValue(new Error('boom'))
    wrap(<CrewPerpetualSection crew="Radar" />)
    expect(await screen.findByTestId('crew-perpetual-load-error')).toBeTruthy()
    expect(screen.queryByTestId('crew-perpetual-switch')).toBeNull()
    expect(screen.queryByTestId('crew-perpetual-reason')).toBeNull()
  })

  it('a gateway with no nudge service withholds the switch and says so, never "never been turned on"', async () => {
    H.autonudgeList.mockResolvedValue({ enabled: false, loops: [] })
    wrap(<CrewPerpetualSection crew="Radar" />)
    const reason = await screen.findByTestId('crew-perpetual-reason')
    // Plain product wording, no "gateway"; no invented settings path either.
    expect(reason.textContent).toMatch(/Perpetual mode is turned off on this install/i)
    expect(reason.textContent).not.toMatch(/gateway/i)
    expect(reason.textContent).not.toMatch(/never been turned on/i)
    expect(screen.queryByTestId('crew-perpetual-switch')).toBeNull()
    expect(H.memberPerpetualSet).not.toHaveBeenCalled()
  })

  it("a restart stop (the service's `interrupted_cycle`) reads as the restart sentence, not the raw code", async () => {
    H.members.mockResolvedValue({ members: [{ ...ROW, perpetual: 'off' }] })
    H.autonudgeList.mockResolvedValue({
      enabled: true,
      loops: [{ ...LOOP, active: false, stopped_reason: 'interrupted_cycle', next_due_ts: 0 }],
    })
    wrap(<CrewPerpetualSection crew="Radar" />)
    const reason = await screen.findByTestId('crew-perpetual-reason')
    expect(reason.textContent).toMatch(/restart/i)
    expect(reason.textContent).not.toMatch(/interrupted_cycle/)
  })

  it('a paused record with no reason is not attributed to the owner', async () => {
    H.members.mockResolvedValue({ members: [{ ...ROW, perpetual: 'off' }] })
    H.autonudgeList.mockResolvedValue({
      enabled: true,
      loops: [{ ...LOOP, active: false, stopped_reason: '', next_due_ts: 0 }],
    })
    wrap(<CrewPerpetualSection crew="Radar" />)
    const reason = await screen.findByTestId('crew-perpetual-reason')
    expect(reason.textContent).toMatch(/no reason recorded/i)
    expect(reason.textContent).not.toMatch(/turned off by you/i)
  })

  it('withholds the switch for a crew the roster does not name', async () => {
    H.autonudgeList.mockResolvedValue({ enabled: true, loops: [LOOP] })
    wrap(<CrewPerpetualSection crew="Nobody" />)
    await screen.findByTestId('crew-perpetual-status')
    expect(screen.queryByTestId('crew-perpetual-switch')).toBeNull()
    expect(screen.getByTestId('crew-perpetual-reason').textContent).toMatch(/never been turned on/)
  })

  it('only the exact roster name resolves the slot -- a slug twin does not borrow the loop', async () => {
    H.members.mockResolvedValue({ members: [ROW, { name: 'radar', slug: 'radar', slot_key: '', running: false }] })
    H.autonudgeList.mockResolvedValue({ enabled: true, loops: [LOOP] })
    wrap(<CrewPerpetualSection crew="radar" />)
    await waitFor(() => expect(switchEl().getAttribute('aria-disabled')).toBe('true'))
    expect(screen.getByTestId('crew-perpetual-status').getAttribute('data-state')).toBe('none')
  })
})
