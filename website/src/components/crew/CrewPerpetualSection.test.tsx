import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

/**
 * The crewmate's Perpetual mode switch on its detail page.
 *
 * What is pinned: the two layers under the switch -- the facts that decide the
 * press in body copy and as the switch's own description, the rest muted below
 * them; the switch's position is the registry READ, never the press
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
    const status = screen.getByTestId('crew-perpetual-status')
    expect(status.getAttribute('data-state')).toBe('on')
    expect(status.textContent).toMatch(/^On\. Checking on its own\./)
    expect(status.querySelectorAll('dl > div')).toHaveLength(4)
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
  })

  it('states the facts that decide the press in body copy, and only those, as the switch\'s description', async () => {
    H.autonudgeList.mockResolvedValue({ enabled: true, loops: [LOOP] })
    wrap(<CrewPerpetualSection crew="Radar" />)
    await waitFor(() => expect(switchEl().getAttribute('aria-checked')).toBe('true'))

    // Two layers, not one muted paragraph: a label/value list carries the three
    // facts a reader needs before pressing (when it starts, what it costs, and
    // where to review what it will act on), and the muted lines below carry
    // the rest.
    const facts = screen.getByTestId('crew-perpetual-facts')
    expect(facts.tagName).toBe('DL')
    expect(facts.className).not.toContain('text-muted')
    expect(facts.querySelectorAll('dt')).toHaveLength(3)
    expect(facts.querySelectorAll('dd')).toHaveLength(3)
    // The switch is described by the primary facts ALONE -- the secondary lines
    // are page text in reading order, not a paragraph read out on every focus.
    // The id must resolve to exactly this list: a duplicate id would make the
    // description ambiguous, and an id nothing carries would describe nothing.
    expect(switchEl().getAttribute('aria-describedby')).toBe('crew-perpetual-facts')
    expect(document.querySelectorAll('#crew-perpetual-facts')).toHaveLength(1)
    expect(document.getElementById('crew-perpetual-facts')).toBe(facts)

    const firstWake = screen.getByTestId('crew-perpetual-fact-first-wake')
    // Scope leads: the mode continues only work already asked for in this
    // crewmate's chat, then states the next check and safe idle case.
    expect(firstWake.textContent).toMatch(
      /^It only continues work you already asked for in this crewmate's chat\. About 1\s?h after you turn it on, it checks that work again\. If nothing is due, nothing happens\.$/,
    )
    // Cost uses the user's unit, a message, never model machinery. 3600s → 24/day.
    expect(screen.getByTestId('crew-perpetual-fact-each-wake').textContent).toBe(
      'Turn it off anytime to stop new checks. Each check costs about what one reply in its chat costs. At this interval, that can be about 24 messages a day.',
    )
    // Review: the thing an uncapped loop will act on is the crewmate's own
    // chat, so the third fact is a link to that chat, addressed by the roster
    // name (not the slug) the section was given.
    const reviewChat = screen.getByTestId('crew-perpetual-review-chat')
    expect(reviewChat.tagName).toBe('A')
    expect(reviewChat.textContent).toBe("Open its chat")
    expect(reviewChat).toHaveAttribute('href', '/members?member=Radar')
    // Targets a new browsing context and sends no referrer; the attributes are
    // what is checked here, not what a browser does with them.
    expect(reviewChat).toHaveAttribute('target', '_blank')
    expect(reviewChat).toHaveAttribute('rel', 'noreferrer')
    // The link is the THIRD fact's value: inside the third `dd`, which shares
    // its row with the third `dt` ("Before turning on"), so a screen reader
    // pairs the label with the link rather than reading a stray anchor.
    const dts = [...facts.querySelectorAll('dt')]
    const dds = [...facts.querySelectorAll('dd')]
    expect(dds[2].contains(reviewChat)).toBe(true)
    expect(dds[0].contains(reviewChat)).toBe(false)
    expect(dds[1].contains(reviewChat)).toBe(false)
    expect(dds[2].parentElement).toBe(dts[2].parentElement)
    expect(dts[2].nextElementSibling).toBe(dds[2])
    // Each fact wears its own label, so the list reads as label/value to a
    // screen reader instead of three sentences in a row.
    const labels = dts.map((dt) => dt.textContent)
    expect(labels).toEqual(['What it does', 'What it costs', 'Before turning on'])

    // The secondary layer is muted and keeps every fact that does NOT decide
    // the press: no cap on count or duration, what OFF does and does not stop,
    // and that scheduled jobs are separate work either way.
    const limits = screen.getByTestId('crew-perpetual-limits')
    expect(limits.className).toContain('text-muted')
    expect(limits.textContent).toMatch(/There is no set end\./)
    expect(limits.textContent).toMatch(/Turn it off here to stop new checks at once; work already running finishes\./)
    expect(limits.textContent).toMatch(/Scheduled jobs run separately\./)
    // The cadence is measured from the END of a check (never a frequency), the
    // interval is the crewmate's own to retune, and the two saving controls on
    // this screen are told apart.
    const cadence = screen.getByTestId('crew-perpetual-cadence')
    expect(cadence.className).toContain('text-muted')
    expect(cadence.textContent).toMatch(/Each later interval starts when the current check ends/)
    expect(cadence.textContent).toMatch(/the crewmate can retune that interval from inside a check/)
    expect(cadence.textContent).not.toMatch(/Save changes/)
    // Neither muted line is inside the list the switch is described by, so
    // neither is read out as part of the switch's description.
    expect(facts.contains(limits)).toBe(false)
    expect(facts.contains(cadence)).toBe(false)
    const all = `${facts.textContent} ${limits.textContent} ${cadence.textContent}`
    expect(all).not.toMatch(/how often/)
    // There is no Perpetual interval editor on this page, so nothing here
    // offers the reader an interval to set or save.
    expect(all).not.toMatch(/\byou (can )?(set|change|save)s? (the |its )?interval\b/i)
  })

  it('states the first-wake timing and the per-day cost from the default interval when no loop exists yet', async () => {
    // Nothing armed: the facts still have to answer "when does it start and
    // what does it cost", so they fall back to the interval an arm begins on
    // (1 h, the backend's own default) rather than going blank or reading 0.
    H.autonudgeList.mockResolvedValue({ enabled: true, loops: [] })
    wrap(<CrewPerpetualSection crew="Radar" />)
    await screen.findByTestId('crew-perpetual-facts')
    expect(screen.getByTestId('crew-perpetual-fact-first-wake').textContent).toMatch(/About 1\s?h after you turn it on/)
    expect(screen.getByTestId('crew-perpetual-fact-each-wake').textContent).toMatch(/about 24 messages a day/)
  })

  it('states the per-day cost from the interval the crewmate retuned itself', async () => {
    // The crewmate owns the cadence (`monitor_update`), so a retuned interval
    // must move the estimate with it: 15 minutes is 96 wakes a day.
    H.autonudgeList.mockResolvedValue({ enabled: true, loops: [{ ...LOOP, idle_secs: 900 }] })
    wrap(<CrewPerpetualSection crew="Radar" />)
    await screen.findByTestId('crew-perpetual-facts')
    expect(screen.getByTestId('crew-perpetual-fact-each-wake').textContent).toMatch(/about 96 messages a day/)
    expect(screen.getByTestId('crew-perpetual-fact-first-wake').textContent).toMatch(/About 15\s?m after you turn it on/)
  })

  it('withholds the explainer when the switch is not offered, so nothing describes a press that cannot happen', async () => {
    // A gateway with no nudge service: the block says the mode is off for this
    // install, and the facts about turning it on are not rendered at all --
    // including as the switch's description, which would dangle.
    H.autonudgeList.mockResolvedValue({ enabled: false, loops: [] })
    wrap(<CrewPerpetualSection crew="Radar" />)
    await screen.findByTestId('crew-perpetual-reason')
    expect(screen.queryByTestId('crew-perpetual-facts')).toBeNull()
    expect(screen.queryByTestId('crew-perpetual-limits')).toBeNull()
    expect(screen.queryByTestId('crew-perpetual-cadence')).toBeNull()
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
    const status = screen.getByTestId('crew-perpetual-status')
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
    // The card is one persistent layout slot. Only its overlapping keyed layer
    // changes, so ON cannot pass through an empty card between OFF and ON.
    expect(screen.getByTestId('crew-perpetual-status')).toBe(status)
    expect(status.textContent).toMatch(/^On\. Checking on its own\./)
    expect(status).not.toHaveTextContent(/Perpetual mode has never been turned on/)
    expect(status.querySelectorAll('[data-testid="crew-perpetual-status-layer"]')).toHaveLength(1)
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
    expect((await screen.findByTestId('crew-perpetual-error')).textContent).toMatch(/repeating task from its chat.*stop the task there first/)
    expect(switchEl().getAttribute('aria-checked')).toBe('false')
    expect(switchEl().getAttribute('aria-disabled')).toBeNull()
    // No agent hand-off on the refusal, ever: `ErrorNotice`'s hand-off opens a
    // NEW /chat, not this crewmate's, and the remedy the sentence names (open
    // its chat, stop the task there) is the direct link already on screen in
    // the fact list above. One "Open its chat" on the page, and it is truthful.
    const notice = screen.getByTestId('crew-perpetual-error')
    expect(notice.querySelector('button')).toBeNull()
    const chatLinks = screen.getAllByText('Open its chat')
    expect(chatLinks).toHaveLength(1)
    expect(chatLinks[0]).toBe(screen.getByTestId('crew-perpetual-review-chat'))
    expect(chatLinks[0].tagName).toBe('A')
    expect(chatLinks[0]).toHaveAttribute('href', '/members?member=Radar')
  })

  it('an uncoded refusal shows plain copy and no agent hand-off', async () => {
    H.autonudgeList.mockResolvedValue({ enabled: true, loops: [LOOP] })
    H.memberPerpetualSet.mockRejectedValue(Object.assign(new Error('trust record unreadable'), { status: 503, body: '' }))
    wrap(<CrewPerpetualSection crew="Radar" />)
    await waitFor(() => expect(switchEl().getAttribute('aria-checked')).toBe('true'))
    fireEvent.click(switchEl())
    const notice = await screen.findByTestId('crew-perpetual-error')
    expect(notice).toHaveTextContent(/Try again\. If it still fails/)
    expect(notice).not.toHaveTextContent(/trust record unreadable/)
    expect(notice.querySelector('button')).toBeNull()
    // The direct crewmate-chat link stays where it was, above the notice.
    expect(screen.getByTestId('crew-perpetual-review-chat')).toHaveAttribute('href', '/members?member=Radar')
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
      loops: [{ id: 'mon1', slot_key: 'member-radar', active: true, idle_secs: 300, next_due_ts: now + 200, gate: false, record_kind: 'structured_monitor' }],
    })
    wrap(<CrewPerpetualSection crew="Radar" />)
    await waitFor(() => expect(switchEl().getAttribute('aria-checked')).toBe('false'))
    expect(screen.getByTestId('crew-perpetual-status').getAttribute('data-state')).toBe('none')
    // Named for what it is, not "never turned on": the reader sees why the
    // switch is off and gets the direct stop path without first pressing a
    // control the monitor blocks.
    const reason = screen.getByTestId('crew-perpetual-reason').textContent ?? ''
    expect(reason).toMatch(/^off\..*repeating task it set up in its chat is running instead.*open its chat.*stop the task there first/i)
    expect(screen.getByTestId('crew-perpetual-monitor-chat')).toHaveAttribute('href', '/members?member=Radar')
    // Opens in a new tab so a Schedules navigation cannot unmount JobForm
    // and lose an unsaved draft.
    expect(screen.getByTestId('crew-perpetual-monitor-chat')).toHaveAttribute('target', '_blank')
    expect(screen.getByTestId('crew-perpetual-monitor-chat')).toHaveAttribute('rel', 'noreferrer')
    expect(screen.getByTestId('crew-perpetual-monitor-chat')).toHaveTextContent('Open its chat')
    expect(screen.queryByTestId('crew-perpetual-next')).toBeNull()
  })

  it('the roster\'s perpetual field decides the state when it disagrees with the record', async () => {
    H.members.mockResolvedValue({ members: [{ ...ROW, perpetual: 'off' }] })
    H.autonudgeList.mockResolvedValue({ enabled: true, loops: [LOOP] })
    wrap(<CrewPerpetualSection crew="Radar" />)
    await waitFor(() => expect(switchEl().getAttribute('aria-checked')).toBe('false'))
    expect(screen.getByTestId('crew-perpetual-status').getAttribute('data-state')).toBe('off')
    expect(screen.getByTestId('crew-perpetual-reason').textContent).toMatch(/updating Kiro Crew turned Perpetual mode off.*turn it back on to resume/i)
  })

  it('roster ON with no registry record yet reads ON with the readouts withheld, never "never been turned on"', async () => {
    // The roster and the registry are two reads: right after a press (or
    // across a dropped frame) the roster can already say ON while the registry
    // still holds no record for the thread. The verdict is the roster's word;
    // only the readouts wait for the record.
    H.members.mockResolvedValue({ members: [{ ...ROW, perpetual: 'on' }] })
    H.autonudgeList.mockResolvedValue({ enabled: true, loops: [] })
    wrap(<CrewPerpetualSection crew="Radar" />)
    await waitFor(() => expect(switchEl().getAttribute('aria-checked')).toBe('true'))
    expect(screen.getByTestId('crew-perpetual-status').getAttribute('data-state')).toBe('on')
    expect(screen.getByTestId('crew-perpetual-verdict').textContent).toMatch(/^On\./)
    expect(screen.queryByTestId('crew-perpetual-reason')).toBeNull()
    expect(screen.queryByText(/never been turned on/)).toBeNull()
    expect(screen.queryByTestId('crew-perpetual-interval')).toBeNull()
    expect(screen.queryByTestId('crew-perpetual-next')).toBeNull()
  })

  it('withholds the switch and never says "nothing wakes it" when the registry read failed', async () => {
    H.autonudgeList.mockRejectedValue(new Error('boom'))
    wrap(<CrewPerpetualSection crew="Radar" />)
    expect(await screen.findByTestId('crew-perpetual-load-error')).toBeTruthy()
    expect(screen.queryByTestId('crew-perpetual-switch')).toBeNull()
    expect(screen.queryByTestId('crew-perpetual-reason')).toBeNull()
    // No agent hand-off on a read failure either: the way out is the retry.
    expect(screen.getByTestId('crew-perpetual-load-error').querySelector('button')).toBeNull()
    expect(screen.getByTestId('crew-perpetual-load-retry')).toHaveTextContent('Try again')
  })

  it('the load-failure retry re-asks BOTH reads and settles on the answer', async () => {
    // Both reads are asked once on mount; the registry fails, the roster
    // answers. The retry must refetch the roster too -- the reading is
    // assembled from both, and the roster row could be as stale as the
    // registry read was unlucky.
    H.autonudgeList.mockRejectedValueOnce(new Error('boom')).mockResolvedValue({ enabled: true, loops: [LOOP] })
    wrap(<CrewPerpetualSection crew="Radar" />)
    await screen.findByTestId('crew-perpetual-load-error')
    expect(H.autonudgeList).toHaveBeenCalledTimes(1)
    expect(H.members).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByTestId('crew-perpetual-load-retry'))
    await waitFor(() => expect(H.autonudgeList).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(H.members.mock.calls.length).toBeGreaterThanOrEqual(2))
    // The second answer replaces the notice with the reading, no reload needed.
    await waitFor(() => expect(switchEl().getAttribute('aria-checked')).toBe('true'))
    expect(screen.queryByTestId('crew-perpetual-load-error')).toBeNull()
    expect(screen.queryByTestId('crew-perpetual-load-retry')).toBeNull()
    expect(screen.getByTestId('crew-perpetual-status').getAttribute('data-state')).toBe('on')
  })

  it('a gateway with no nudge service withholds the switch and says so, never "never been turned on"', async () => {
    H.autonudgeList.mockResolvedValue({ enabled: false, loops: [] })
    wrap(<CrewPerpetualSection crew="Radar" />)
    const reason = await screen.findByTestId('crew-perpetual-reason')
    // Name the person who can act, without a technical "gateway" label.
    expect(reason.textContent).toMatch(/person who runs this Kiro Crew install turned Perpetual mode off/i)
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

  it("a bounds-repair stop (the service's `invalid_bounds`) reads as the saved-limits sentence, not the raw code", async () => {
    H.members.mockResolvedValue({ members: [{ ...ROW, perpetual: 'off' }] })
    H.autonudgeList.mockResolvedValue({
      enabled: true,
      loops: [{ ...LOOP, active: false, stopped_reason: 'invalid_bounds', next_due_ts: 0 }],
    })
    wrap(<CrewPerpetualSection crew="Radar" />)
    const reason = await screen.findByTestId('crew-perpetual-reason')
    expect(reason.textContent).toMatch(/saved limits/i)
    expect(reason.textContent).not.toMatch(/invalid_bounds/)
    expect(reason.textContent).not.toMatch(/no reason recorded/i)
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

  // The service writes codes this section has no dedicated sentence for
  // (`session_start_failures`, `structural_terminal`, `sentinel_dropped`, and
  // whatever comes next). A reason WAS recorded, so "no reason recorded" would
  // misreport the stop; the product sentence carries the code instead -- the
  // same split the Crewmates page's Perpetual mode block makes.
  it.each(['session_start_failures', 'structural_terminal', 'sentinel_dropped', 'future_internal_code'])(
    'an unmapped stop code (%s) renders the product sentence carrying the code, never "no reason"',
    async (code) => {
      H.members.mockResolvedValue({ members: [{ ...ROW, perpetual: 'off' }] })
      H.autonudgeList.mockResolvedValue({
        enabled: true,
        loops: [{ ...LOOP, active: false, stopped_reason: code, next_due_ts: 0 }],
      })
      wrap(<CrewPerpetualSection crew="Radar" />)
      const reason = await screen.findByTestId('crew-perpetual-reason')
      expect(reason.textContent).toMatch(/cannot explain yet/i)
      expect(reason.textContent).toContain(`(${code})`)
      expect(reason.textContent).not.toMatch(/no reason recorded/i)
    },
  )

  it('an EMPTY stop code reads as no reason recorded, never as the unknown-code sentence', async () => {
    H.members.mockResolvedValue({ members: [{ ...ROW, perpetual: 'off' }] })
    H.autonudgeList.mockResolvedValue({
      enabled: true,
      loops: [{ ...LOOP, active: false, stopped_reason: '', next_due_ts: 0 }],
    })
    wrap(<CrewPerpetualSection crew="Radar" />)
    const reason = await screen.findByTestId('crew-perpetual-reason')
    expect(reason.textContent).toMatch(/no reason recorded/i)
    expect(reason.textContent).not.toMatch(/cannot explain yet/i)
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
