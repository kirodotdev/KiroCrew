import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { useLocation } from 'react-router-dom'
import { renderWithProviders } from '../test/helpers'
import MeetCrewmatesFlow, {
  builtFromOptions,
  formatDailyTime,
  isValidCrewmateName,
  nextRunIsToday,
  parseDailyTime,
  scheduleFor,
} from './MeetCrewmatesFlow'
import { hasNoCrewmates } from '../hooks/useMeetCrewmatesGate'
import { seededTraits } from './CrewAvatar'
import { api } from '../api/client'

// framer-motion never finishes an exit animation in jsdom, so the step
// AnimatePresence (mode="wait") would hold the next step off-screen forever.
// Same pass-through mock the other AnimatePresence consumers' tests use.
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'initial', 'animate', 'exit', 'transition', 'variants',
    'whileHover', 'whileTap', 'whileInView', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef<HTMLElement, Record<string, unknown> & { children?: React.ReactNode }>((props, ref) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children)
    })
  // Cached per tag: a fresh component type on every `motion.div` read would
  // remount the step subtree on each render and detach any element a test holds.
  const cache = new Map<string, ReturnType<typeof make>>()
  const motion = new Proxy({}, {
    get: (_t, tag: string) => {
      if (!cache.has(tag)) cache.set(tag, make(tag))
      return cache.get(tag)
    },
  })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    useReducedMotion: () => true,
  }
})

// Partial api mock: the two writes the Create step performs plus the two reads
// the flow makes while open. Everything else keeps its real implementation
// (ThemeProvider's ancillary fetches no-op in jsdom).
// The When select renders as a native <select> on touch, which jsdom can drive
// with fireEvent.change; the Radix path cannot be opened here. Same pattern as
// CliPanelCoverage.test.tsx.
const touch = { value: false }
vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => touch.value }))

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      themeBoot: vi.fn().mockResolvedValue({ mode: '', color: '', onboarded: true }),
      agentsInstalled: vi.fn().mockResolvedValue([{ name: 'kirocrew', source: 'kirocrew' }]),
      getSlackConfig: vi.fn().mockResolvedValue({ configured: true, connected: false }),
      createKirocrewAgent: vi.fn().mockResolvedValue({ ok: true, name: 'Radar', memory_store: 'm1', member_id: 'radar-id' }),
      members: vi.fn().mockResolvedValue({ members: [] }),
      crons: vi.fn().mockResolvedValue({ jobs: [] }),
      createCron: vi.fn().mockResolvedValue({ ok: true, id: 'job-1' }),
    },
  }
})

const createAgent = vi.mocked(api.createKirocrewAgent)
const createCron = vi.mocked(api.createCron)
const members = vi.mocked(api.members)

const next = () => fireEvent.click(screen.getByTestId('meet-crewmates-next'))
const RADAR_GOAL = 'Keep new GitHub issues triaged and flag those that need a decision'
const setTime = (value: string) => fireEvent.change(screen.getByTestId('meet-crewmates-time'), { target: { value } })
/** The zone the flow captures, read the same way the component reads it. */
const browserZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'

describe('MeetCrewmatesFlow', () => {
  beforeEach(() => {
    createAgent.mockReset()
    createAgent.mockResolvedValue({ ok: true, name: 'Radar', memory_store: 'm1', member_id: 'radar-id' })
    createCron.mockReset()
    createCron.mockResolvedValue({ ok: true, id: 'job-1' })
    members.mockReset()
    members.mockResolvedValue({ members: [] })
    vi.mocked(api.crons).mockReset()
    vi.mocked(api.crons).mockResolvedValue({ jobs: [] })
  })

  it('renders nothing while closed', () => {
    renderWithProviders(<MeetCrewmatesFlow open={false} onDone={vi.fn()} onCreated={vi.fn()} />)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('step 1 shows the three example crewmates and a step counter', () => {
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    expect(screen.getByRole('dialog', { name: 'Meet CrewMates' })).toBeInTheDocument()
    expect(screen.getByText('CrewMates · 1 of 4')).toBeInTheDocument()
    const examples = screen.getByTestId('meet-crewmates-examples')
    expect(examples).toHaveTextContent('Radar')
    expect(examples).toHaveTextContent('Scribe')
    expect(examples).toHaveTextContent('Fixer')
  })

  it('Not now reports a dismissal and never writes', () => {
    const onDone = vi.fn()
    renderWithProviders(<MeetCrewmatesFlow open onDone={onDone} onCreated={vi.fn()} />)
    fireEvent.click(screen.getByTestId('meet-crewmates-not-now'))
    expect(onDone).toHaveBeenCalledWith('dismissed')
    expect(createAgent).not.toHaveBeenCalled()
  })

  it('Escape before the crewmate exists is a dismissal', () => {
    const onDone = vi.fn()
    renderWithProviders(<MeetCrewmatesFlow open onDone={onDone} onCreated={vi.fn()} />)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onDone).toHaveBeenCalledWith('dismissed')
  })

  it('step 2 prefills Radar, a chip swaps the name and the job, and an empty name blocks Next', () => {
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    const name = screen.getByTestId('meet-crewmates-name') as HTMLInputElement
    expect(name.value).toBe('Radar')
    fireEvent.click(screen.getByRole('button', { name: /Scribe/ }))
    expect(name.value).toBe('Scribe')
    fireEvent.change(name, { target: { value: '   ' } })
    expect(screen.getByTestId('meet-crewmates-next')).toBeDisabled()
    fireEvent.change(name, { target: { value: 'Radar' } })
    next()
    expect(screen.getByTestId('meet-crewmates-title')).toHaveTextContent('What should Radar achieve?')
  })

  it('entering a step seats focus on a control INSIDE the new step (never on the outgoing one)', () => {
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    const step1 = screen.getByTestId('meet-crewmates-step-1')
    expect(step1.contains(document.activeElement)).toBe(true)
    next()
    // The seat runs from the incoming step's own mount, so it can only ever
    // land on an element that exists after the step swap.
    const step2 = screen.getByTestId('meet-crewmates-step-2')
    expect(step2.contains(document.activeElement)).toBe(true)
    expect(document.activeElement).not.toBe(document.body)
    next()
    expect(screen.getByTestId('meet-crewmates-step-3').contains(document.activeElement)).toBe(true)
  })

  it('Back returns to the previous step', () => {
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-back'))
    expect(screen.getByTestId('meet-crewmates-title')).toHaveTextContent('Give a crewmate a goal to own')
  })

  it('Create posts the crewmate with the job as its description, then a silent schedule bound to it, persists "done" and keeps the ready step open', async () => {
    const onDone = vi.fn()
    const onCreated = vi.fn()
    renderWithProviders(<MeetCrewmatesFlow open onDone={onDone} onCreated={onCreated} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    await waitFor(() => expect(createAgent).toHaveBeenCalledTimes(1))
    expect(createAgent).toHaveBeenCalledWith({
      name: 'Radar',
      kiro_agent: 'kirocrew',
      description: RADAR_GOAL,
      source: 'kirocrew',
      avatar: { kind: 'ghost', traits: seededTraits('Radar') },
    })
    await waitFor(() => expect(createCron).toHaveBeenCalledTimes(1))
    const cronBody = createCron.mock.calls[0][0] as Record<string, unknown>
    // Bound by the immutable identity the create returned, never the display name.
    expect(cronBody.member_id).toBe('radar-id')
    expect(cronBody.agent).toBe('kirocrew')
    // The untouched daily time is 09:00 in the browser's zone.
    expect(cronBody.cron).toBe('0 9 * * *')
    expect(cronBody.timezone).toBe(browserZone())
    // Delivery is mechanical: a non-silent run rings the bell, opens as the
    // crewmate's chat in the sidebar ("Its own chat" on) and reaches a
    // connected Slack through the runtime's own leg.
    expect(cronBody.silent).toBe(false)
    expect(cronBody.hide_in_chat).toBe(false)
    expect(String(cronBody.message)).toContain(RADAR_GOAL)
    expect(await screen.findByTestId('meet-crewmates-ready')).toHaveTextContent('Radar is ready')
    expect(screen.getByTestId('meet-crewmates-ready-goal')).toHaveTextContent(`Goal: ${RADAR_GOAL}`)
    expect(onCreated).toHaveBeenCalledTimes(1)
    // The host closes on onDone only; the ready step must still be on screen.
    expect(onDone).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('meet-crewmates-open-chat'))
    expect(onDone).toHaveBeenCalledWith('completed')
  })

  it('the ready step also offers a quiet Done that closes without navigating', async () => {
    const onDone = vi.fn()
    renderWithProviders(<MeetCrewmatesFlow open onDone={onDone} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    await screen.findByTestId('meet-crewmates-ready')
    fireEvent.click(screen.getByTestId('meet-crewmates-done'))
    expect(onDone).toHaveBeenCalledWith('completed')
  })

  it('a refused "done" write is shown as an ErrorNotice and the next exit reaches the host again', () => {
    const onDone = vi.fn()
    renderWithProviders(<MeetCrewmatesFlow open onDone={onDone} onCreated={vi.fn()} persistFailed />)
    expect(screen.getByTestId('meet-crewmates-persist-error')).toHaveTextContent('could not be marked as done')
    fireEvent.click(screen.getByTestId('meet-crewmates-not-now'))
    fireEvent.click(screen.getByTestId('meet-crewmates-not-now'))
    expect(onDone).toHaveBeenCalledTimes(2)
  })

  it('a create with no usable answer stops on step 3: nothing is claimed by name, no schedule, the notice sends the user to the Crewmates page', async () => {
    createAgent.mockRejectedValueOnce(new Error('network'))
    // A same-named row on the roster is NOT evidence: another opening prefills
    // the same example name. Without the identity from a clean create response
    // there is nothing this flow may claim.
    members.mockResolvedValue({ members: [{ name: 'Radar', slug: 'radar', memory_store: 'someone-elses' } as never] })
    const onDone = vi.fn()
    renderWithProviders(<MeetCrewmatesFlow open onDone={onDone} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    const notice = await screen.findByTestId('meet-crewmates-error')
    expect(notice).toHaveTextContent('may or may not have been created')
    expect(notice).toHaveTextContent('Crew Members page')
    expect(screen.getByTestId('meet-crewmates-step-3')).toBeInTheDocument()
    expect(createCron).not.toHaveBeenCalled()
    expect(members).not.toHaveBeenCalled()
    expect(screen.queryByTestId('meet-crewmates-ready')).toBeNull()
    fireEvent.click(screen.getByTestId('meet-crewmates-open-crewmates'))
    expect(onDone).toHaveBeenCalledWith('completed')
  })

  it('a 409 after an unanswered create is a taken name, never this flow\'s own crewmate', async () => {
    const { ApiError } = await import('../api/apiError')
    createAgent.mockRejectedValueOnce(new Error('network'))
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    expect(await screen.findByTestId('meet-crewmates-error')).toHaveTextContent('may or may not have been created')
    createAgent.mockRejectedValueOnce(new ApiError(409, 'exists', JSON.stringify({ code: 'agent_exists' })))
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    expect(await screen.findByTestId('meet-crewmates-name-error')).toHaveTextContent('already exists')
    expect(screen.getByTestId('meet-crewmates-step-2')).toBeInTheDocument()
    expect(createCron).not.toHaveBeenCalled()
  })

  it('a create the server refused (4xx) says so inline, with no Crewmates-page button: nothing was made', async () => {
    const { ApiError } = await import('../api/apiError')
    createAgent.mockRejectedValueOnce(new ApiError(403, 'forbidden', '{}'))
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    expect(await screen.findByTestId('meet-crewmates-error')).toHaveTextContent('could not be created')
    expect(screen.queryByTestId('meet-crewmates-open-crewmates')).toBeNull()
    expect(createCron).not.toHaveBeenCalled()
  })

  it('a taken name sends the user back to step 2 with the error under the name field, no completion', async () => {
    const { ApiError } = await import('../api/apiError')
    createAgent.mockRejectedValue(new ApiError(409, 'exists', JSON.stringify({ code: 'agent_exists' })))
    const onDone = vi.fn()
    renderWithProviders(<MeetCrewmatesFlow open onDone={onDone} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    expect(await screen.findByTestId('meet-crewmates-name-error')).toHaveTextContent('A crewmate named Radar already exists')
    expect(screen.getByTestId('meet-crewmates-step-2')).toBeInTheDocument()
    expect(screen.getByTestId('meet-crewmates-name')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByTestId('meet-crewmates-next')).toBeDisabled()
    expect(createCron).not.toHaveBeenCalled()
    expect(onDone).not.toHaveBeenCalled()
    // The notice carries the way to the crewmate that owns the name, and the
    // matching suggestion chip is marked as taken.
    expect(screen.getByTestId('meet-crewmates-name-taken-open')).toHaveTextContent('Open the Crew Members page')
    expect(screen.getByRole('button', { name: /^Radar · already exists$/ })).toHaveAttribute('title', 'already exists')
    // Picking another chip is a name change too: the refusal clears, Next comes back.
    fireEvent.click(screen.getByRole('button', { name: /Scribe/ }))
    expect(screen.queryByTestId('meet-crewmates-name-error')).toBeNull()
    expect(screen.getByTestId('meet-crewmates-next')).toBeEnabled()
    // Back to the refused name, then typing a new one clears it as well.
    fireEvent.change(screen.getByTestId('meet-crewmates-name'), { target: { value: 'Radar2' } })
    expect(screen.queryByTestId('meet-crewmates-name-error')).toBeNull()
    expect(screen.queryByTestId('meet-crewmates-name-taken-open')).toBeNull()
    expect(screen.getByTestId('meet-crewmates-next')).toBeEnabled()
    // Leaving through the notice's button reports a dismissal (no crewmate was made).
    createAgent.mockRejectedValue(new ApiError(409, 'exists', JSON.stringify({ code: 'agent_exists' })))
    fireEvent.change(screen.getByTestId('meet-crewmates-name'), { target: { value: 'Radar' } })
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    fireEvent.click(await screen.findByTestId('meet-crewmates-name-taken-open'))
    expect(onDone).toHaveBeenCalledWith('dismissed')
  })

  it('a schedule the server refused (4xx) lands on the ready step saying it was not saved', async () => {
    const { ApiError } = await import('../api/apiError')
    createCron.mockRejectedValue(new ApiError(400, 'invalid_cron', '{}'))
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    expect(await screen.findByTestId('meet-crewmates-ready')).toHaveTextContent('Radar is ready')
    expect(screen.getByTestId('meet-crewmates-schedule-error')).toHaveTextContent('its schedule was not saved')
    // No invented next run on a failed schedule: only the goal and the notice.
    expect(screen.getByTestId('meet-crewmates-ready-goal')).toHaveTextContent(`Goal: ${RADAR_GOAL}`)
    expect(screen.queryByTestId('meet-crewmates-ready-starts')).toBeNull()
    expect(screen.getByTestId('meet-crewmates-ready')).not.toHaveTextContent('Next run')
  })

  it('a schedule write with no answer is reconciled against the Schedule list: only the exact job asked for means saved', async () => {
    createCron.mockRejectedValueOnce(new Error('network'))
    // Same identity, same name, same message, same schedule: this IS the job.
    vi.mocked(api.crons).mockResolvedValueOnce({ jobs: [{ id: 'j1', name: 'Radar: standing job', message: RADAR_GOAL, member_id: 'radar-id', cron_expr: '0 9 * * *' } as never] })
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    expect(await screen.findByTestId('meet-crewmates-ready')).toHaveTextContent('Radar is ready')
    expect(screen.queryByTestId('meet-crewmates-schedule-error')).toBeNull()
    expect(screen.getByTestId('meet-crewmates-ready-starts')).toHaveTextContent('Next run:')
  })

  it('a schedule write with no answer and no job on the Schedule list is reported as maybe-unsaved, pointing at the Schedule page', async () => {
    createCron.mockRejectedValue(new Error('network'))
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    expect(await screen.findByTestId('meet-crewmates-ready')).toHaveTextContent('Radar is ready')
    expect(screen.getByTestId('meet-crewmates-schedule-error')).toHaveTextContent('may not have been saved')
  })

  it('a schedule reconcile ignores another job on the crewmate (other message or schedule) and one on another identity: maybe-unsaved', async () => {
    createCron.mockRejectedValueOnce(new Error('network'))
    vi.mocked(api.crons).mockResolvedValueOnce({ jobs: [
      // Right identity and name, but not the schedule asked for: an older job.
      { id: 'j1', name: 'Radar: standing job', message: RADAR_GOAL, member_id: 'radar-id', every_secs: 3600 } as never,
      // Right identity, another job entirely.
      { id: 'j2', name: 'Radar: standing job', message: 'Something else', member_id: 'radar-id', cron_expr: '0 9 * * *' } as never,
      // The exact job, but on a same-named crewmate with another identity.
      { id: 'j3', name: 'Radar: standing job', message: RADAR_GOAL, member_id: 'radar', cron_expr: '0 9 * * *' } as never,
    ] })
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    expect(await screen.findByTestId('meet-crewmates-ready')).toHaveTextContent('Radar is ready')
    expect(screen.getByTestId('meet-crewmates-schedule-error')).toHaveTextContent('may not have been saved')
  })

  it('"Only when I ask" posts no schedule and step 4 says nothing about reports or the Schedule page', async () => {
    touch.value = true
    try {
      renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
      next()
      next()
      fireEvent.change(screen.getByRole('combobox', { name: 'Run' }), { target: { value: 'ask' } })
      fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    } finally {
      touch.value = false
    }
    const ready = await screen.findByTestId('meet-crewmates-ready')
    expect(createCron).not.toHaveBeenCalled()
    expect(screen.getByTestId('meet-crewmates-ready-starts')).toHaveTextContent('Send a message to start working on this goal.')
    expect(ready).not.toHaveTextContent('Schedule page')
    expect(screen.queryByTestId('meet-crewmates-schedule-error')).toBeNull()
    expect(screen.getByTestId('meet-crewmates-ready-goal')).toHaveTextContent(`Goal: ${RADAR_GOAL}`)
  })

  describe('daily time', () => {
    const toStep3 = () => {
      renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
      next()
      next()
    }

    it('the daily choice reads "Every day" and offers a labelled native time input (09:00, 44px, with the zone)', () => {
      touch.value = true
      try {
        toStep3()
        const when = screen.getByRole('combobox', { name: 'Run' }) as HTMLSelectElement
        // The stored value stays `morning` for compatibility; the label is new.
        expect(when.value).toBe('morning')
        expect(when.options[when.selectedIndex].textContent).toBe('Every day')
      } finally {
        touch.value = false
      }
      const time = screen.getByLabelText('Time') as HTMLInputElement
      expect(time).toBe(screen.getByTestId('meet-crewmates-time'))
      expect(time.type).toBe('time')
      expect(time.value).toBe('09:00')
      expect(time.className).toContain('min-h-[44px]')
      expect(screen.getByTestId('meet-crewmates-timezone')).toHaveTextContent(`Time zone: ${browserZone()}`)
      expect(time.getAttribute('aria-describedby')).toContain('meet-crewmates-timezone')
    })

    it('a custom time becomes the cron minute and hour, in the captured zone', async () => {
      toStep3()
      setTime('07:05')
      fireEvent.click(screen.getByTestId('meet-crewmates-create'))
      await waitFor(() => expect(createCron).toHaveBeenCalledTimes(1))
      const body = createCron.mock.calls[0][0] as Record<string, unknown>
      expect(body.cron).toBe('5 7 * * *')
      expect(body.timezone).toBe(browserZone())
    })

    it('a selected late-night time disables jitter so the run stays at that time', async () => {
      toStep3()
      setTime('23:30')
      fireEvent.click(screen.getByTestId('meet-crewmates-create'))
      await waitFor(() => expect(createCron).toHaveBeenCalledTimes(1))
      expect(createCron.mock.calls[0][0]).toMatchObject({
        cron: '30 23 * * *', timezone: browserZone(), strict_schedule: true,
      })
    })

    it('midnight and the last minute of the day map to their own cron fields', async () => {
      toStep3()
      setTime('00:00')
      fireEvent.click(screen.getByTestId('meet-crewmates-create'))
      await waitFor(() => expect(createCron).toHaveBeenCalledTimes(1))
      expect((createCron.mock.calls[0][0] as Record<string, unknown>).cron).toBe('0 0 * * *')
    })

    it('an empty daily time blocks the button, Enter in either field, and every write', async () => {
      toStep3()
      setTime('')
      expect(screen.getByTestId('meet-crewmates-create')).toBeDisabled()
      expect(screen.getByTestId('meet-crewmates-time-error')).toHaveTextContent('Choose a valid time.')
      expect(screen.getByTestId('meet-crewmates-time')).toHaveAttribute('aria-invalid', 'true')
      fireEvent.click(screen.getByTestId('meet-crewmates-create'))
      fireEvent.keyDown(screen.getByTestId('meet-crewmates-job'), { key: 'Enter' })
      fireEvent.keyDown(screen.getByTestId('meet-crewmates-time'), { key: 'Enter' })
      // Give any stray mutation a chance to start before asserting none did.
      await new Promise(r => setTimeout(r, 0))
      expect(createAgent).not.toHaveBeenCalled()
      expect(createCron).not.toHaveBeenCalled()
      // A valid time clears the hint and the button comes back; Enter now creates.
      setTime('18:30')
      expect(screen.queryByTestId('meet-crewmates-time-error')).toBeNull()
      expect(screen.getByTestId('meet-crewmates-create')).toBeEnabled()
      fireEvent.keyDown(screen.getByTestId('meet-crewmates-time'), { key: 'Enter' })
      await waitFor(() => expect(createCron).toHaveBeenCalledTimes(1))
      expect((createCron.mock.calls[0][0] as Record<string, unknown>).cron).toBe('30 18 * * *')
    })

    it('an invalid daily time does not affect hourly or on-demand', async () => {
      touch.value = true
      try {
        toStep3()
        setTime('')
        expect(screen.getByTestId('meet-crewmates-create')).toBeDisabled()
        fireEvent.change(screen.getByRole('combobox', { name: 'Run' }), { target: { value: 'hourly' } })
        expect(screen.queryByTestId('meet-crewmates-time')).toBeNull()
        expect(screen.getByTestId('meet-crewmates-create')).toBeEnabled()
        fireEvent.change(screen.getByRole('combobox', { name: 'Run' }), { target: { value: 'ask' } })
        expect(screen.getByTestId('meet-crewmates-create')).toBeEnabled()
        fireEvent.change(screen.getByRole('combobox', { name: 'Run' }), { target: { value: 'hourly' } })
        fireEvent.click(screen.getByTestId('meet-crewmates-create'))
      } finally {
        touch.value = false
      }
      await waitFor(() => expect(createCron).toHaveBeenCalledTimes(1))
      const body = createCron.mock.calls[0][0] as Record<string, unknown>
      expect(body.every).toBe(3600)
      expect(body.cron).toBeUndefined()
    })

    it('the picked time survives Back and is reset on the next opening', () => {
      const { rerender } = renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
      next()
      next()
      setTime('21:15')
      fireEvent.click(screen.getByTestId('meet-crewmates-back'))
      next()
      expect((screen.getByTestId('meet-crewmates-time') as HTMLInputElement).value).toBe('21:15')
      rerender(<MeetCrewmatesFlow open={false} onDone={vi.fn()} onCreated={vi.fn()} />)
      rerender(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
      next()
      next()
      expect((screen.getByTestId('meet-crewmates-time') as HTMLInputElement).value).toBe('09:00')
    })

    it('every step-3 control is disabled while the create is in flight', async () => {
      let release: (v: { ok: boolean; name: string; memory_store: string; member_id: string }) => void = () => {}
      createAgent.mockImplementationOnce(() => new Promise(r => { release = r }))
      toStep3()
      fireEvent.click(screen.getByTestId('meet-crewmates-create'))
      await waitFor(() => expect(screen.getByTestId('meet-crewmates-time')).toBeDisabled())
      expect(screen.getByTestId('meet-crewmates-job')).toBeDisabled()
      expect(screen.getByTestId('meet-crewmates-create')).toBeDisabled()
      expect(screen.getByTestId('meet-crewmates-back')).toBeDisabled()
      const reportSwitch = screen.getByRole('switch', { name: 'Its own chat' })
      expect(reportSwitch).toHaveAttribute('aria-disabled', 'true')
      fireEvent.click(reportSwitch)
      expect(reportSwitch).toHaveAttribute('aria-checked', 'true')
      release({ ok: true, name: 'Radar', memory_store: 'm1', member_id: 'radar-id' })
      await screen.findByTestId('meet-crewmates-ready')
    })

    describe('ready step next run', () => {
      afterEach(() => {
        vi.useRealTimers()
      })
      // Only Date is faked, so react-query and waitFor keep their real timers.
      const at = (h: number, m: number, s = 0) => {
        const d = new Date()
        d.setHours(h, m, s, 0)
        vi.useFakeTimers({ toFake: ['Date'] })
        vi.setSystemTime(d)
      }
      const createAt = async (time: string) => {
        toStep3()
        setTime(time)
        fireEvent.click(screen.getByTestId('meet-crewmates-create'))
        return screen.findByTestId('meet-crewmates-ready-starts')
      }

      it('a time still ahead today says today, with the picked time and zone', async () => {
        at(10, 29, 59)
        const line = await createAt('10:30')
        expect(line).toHaveTextContent(`Next run: today at ${formatDailyTime('10:30')} (${browserZone()}).`)
      })

      it('the exact current minute has already fired: tomorrow', async () => {
        at(10, 30, 0)
        const line = await createAt('10:30')
        expect(line).toHaveTextContent(`Next run: tomorrow at ${formatDailyTime('10:30')} (${browserZone()}).`)
      })

      it('a time earlier today says tomorrow', async () => {
        at(23, 0)
        const line = await createAt('09:00')
        expect(line).toHaveTextContent(`Next run: tomorrow at ${formatDailyTime('09:00')}`)
      })
    })
  })

  it('the ready step shows the goal as plain text, never markup', async () => {
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.change(screen.getByTestId('meet-crewmates-job'), { target: { value: '<b>Ship</b> & tell' } })
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    const goal = await screen.findByTestId('meet-crewmates-ready-goal')
    expect(goal).toHaveTextContent('Goal: <b>Ship</b> & tell')
    expect(goal.querySelector('b')).toBeNull()
  })

  it.each(['Issue Radar', '雷达', '-radar'])('a free-form name (%s) is accepted and sent as typed', async name => {
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    fireEvent.change(screen.getByTestId('meet-crewmates-name'), { target: { value: name } })
    expect(screen.getByTestId('meet-crewmates-next')).toBeEnabled()
    expect(screen.getByTestId('meet-crewmates-name')).not.toHaveAttribute('aria-invalid')
    expect(screen.queryByTestId('meet-crewmates-name-hint')).toBeNull()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    await screen.findByTestId('meet-crewmates-ready')
    expect(createAgent).toHaveBeenCalledWith(expect.objectContaining({ name }))
  })

  it('open chat addresses the crewmate by the key the server derived, not the typed label', async () => {
    createAgent.mockResolvedValueOnce({ ok: true, name: 'issue-radar', memory_store: 'm1', member_id: 'ir-id' })
    function Where() {
      const loc = useLocation()
      return <div data-testid="where">{loc.pathname + loc.search}</div>
    }
    renderWithProviders(
      <>
        <MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />
        <Where />
      </>,
    )
    next()
    fireEvent.change(screen.getByTestId('meet-crewmates-name'), { target: { value: 'Issue Radar' } })
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    // The label stays what the user typed.
    expect(await screen.findByTestId('meet-crewmates-ready')).toHaveTextContent('Issue Radar is ready')
    fireEvent.click(screen.getByTestId('meet-crewmates-open-chat'))
    expect(screen.getByTestId('where')).toHaveTextContent('/members?member=issue-radar')
    // The face step 2 previewed (drawn from the typed name) is pinned, so the
    // roster, which draws an unpinned crew from its key, shows the same face.
    expect(createAgent).toHaveBeenCalledWith(
      expect.objectContaining({ avatar: { kind: 'ghost', traits: seededTraits('Issue Radar') } }),
    )
  })

  it('a blank name disables Next', () => {
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    fireEvent.change(screen.getByTestId('meet-crewmates-name'), { target: { value: '   ' } })
    expect(screen.getByTestId('meet-crewmates-next')).toBeDisabled()
  })

  it.each(['invalid_member_name', 'credential_shaped_name'])('a server 400 %s lands under the name field on step 2', async code => {
    const { ApiError } = await import('../api/apiError')
    createAgent.mockRejectedValueOnce(new ApiError(400, 'bad', JSON.stringify({ code })))
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    expect(await screen.findByTestId('meet-crewmates-name-error')).toHaveTextContent("This name can't be used")
    expect(screen.getByTestId('meet-crewmates-step-2')).toBeInTheDocument()
    expect(createCron).not.toHaveBeenCalled()
  })

  it('isValidCrewmateName only refuses a blank name', () => {
    expect(isValidCrewmateName('Radar')).toBe(true)
    expect(isValidCrewmateName('Issue Radar')).toBe(true)
    expect(isValidCrewmateName('雷达')).toBe(true)
    expect(isValidCrewmateName('')).toBe(false)
    expect(isValidCrewmateName('  ')).toBe(false)
  })

  it('a schedule failure notice offers a way to the Schedule page and leaving completes the flow', async () => {
    const { ApiError } = await import('../api/apiError')
    createCron.mockRejectedValue(new ApiError(400, 'invalid_cron', '{}'))
    const onDone = vi.fn()
    renderWithProviders(<MeetCrewmatesFlow open onDone={onDone} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.click(screen.getByTestId('meet-crewmates-create'))
    await screen.findByTestId('meet-crewmates-ready')
    fireEvent.click(screen.getByTestId('meet-crewmates-open-schedule'))
    expect(onDone).toHaveBeenCalledWith('completed')
  })

  it('a step-1 example row starts the flow with that crewmate: name and job preselected, step 2 open', () => {
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Start with Scribe' }))
    expect(screen.getByTestId('meet-crewmates-step-2')).toBeInTheDocument()
    expect((screen.getByTestId('meet-crewmates-name') as HTMLInputElement).value).toBe('Scribe')
    next()
    expect((screen.getByTestId('meet-crewmates-job') as HTMLInputElement).value).not.toBe('')
  })

  it('a suggestion chip never overwrites a job the user typed', () => {
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    next()
    fireEvent.change(screen.getByTestId('meet-crewmates-job'), { target: { value: 'Water the plants' } })
    fireEvent.click(screen.getByTestId('meet-crewmates-back'))
    fireEvent.click(screen.getByRole('button', { name: /Scribe/ }))
    next()
    expect((screen.getByTestId('meet-crewmates-job') as HTMLInputElement).value).toBe('Water the plants')
  })

  it('a failed custom-agent read shows an ErrorNotice on step 2 and still offers the standard setup', async () => {
    vi.mocked(api.agentsInstalled).mockRejectedValueOnce(new Error('boom'))
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    expect(await screen.findByTestId('meet-crewmates-built-from-error')).toHaveTextContent('Could not load your other setups')
    expect(screen.getByText('Standard (built in)')).toBeInTheDocument()
  })

  it('the Slack row is status, not a switch: "Off" with the reason while Slack is not connected', () => {
    renderWithProviders(<MeetCrewmatesFlow open onDone={vi.fn()} onCreated={vi.fn()} />)
    next()
    next()
    expect(screen.queryByRole('switch', { name: 'Slack DM' })).toBeNull()
    const state = screen.getByTestId('meet-crewmates-slack-state')
    expect(state).toHaveTextContent('Off')
    // The state word is part of the label line ("Slack DM — Off"), never a
    // lone word in the column the toggle above occupies.
    expect(state.parentElement).toHaveTextContent('Slack DM — Off')
    expect(screen.getByTestId('meet-crewmates-slack-hint')).toHaveTextContent('Off until Slack is connected in Settings')
  })
})

describe('MeetCrewmatesFlow helpers', () => {
  it('scheduleFor maps the When choice to a cron body', () => {
    // Default time is 09:00.
    expect(scheduleFor('morning', 'Asia/Shanghai')).toEqual({ cron: '0 9 * * *', timezone: 'Asia/Shanghai', strict_schedule: true })
    expect(scheduleFor('morning', 'Europe/Berlin', '07:05')).toEqual({ cron: '5 7 * * *', timezone: 'Europe/Berlin', strict_schedule: true })
    expect(scheduleFor('morning', 'UTC', '00:00')).toEqual({ cron: '0 0 * * *', timezone: 'UTC', strict_schedule: true })
    expect(scheduleFor('morning', 'UTC', '23:59')).toEqual({ cron: '59 23 * * *', timezone: 'UTC', strict_schedule: true })
    expect(scheduleFor('hourly', 'UTC')).toEqual({ every: 3600 })
    expect(scheduleFor('ask', 'UTC')).toBeNull()
    // The time is irrelevant, even when invalid, for hourly and on-demand.
    expect(scheduleFor('hourly', 'UTC', '')).toEqual({ every: 3600 })
    expect(scheduleFor('ask', 'UTC', 'nope')).toBeNull()
  })

  it('scheduleFor refuses an invalid daily time instead of falling back to another hour', () => {
    for (const bad of ['', '9:00', '24:00', '12:60', '12:30:00', 'noon']) {
      expect(() => scheduleFor('morning', 'UTC', bad)).toThrow()
    }
  })

  it('parseDailyTime accepts only HH:mm', () => {
    expect(parseDailyTime('09:00')).toEqual({ hour: 9, minute: 0 })
    expect(parseDailyTime('23:59')).toEqual({ hour: 23, minute: 59 })
    expect(parseDailyTime('')).toBeNull()
    expect(parseDailyTime('9:5')).toBeNull()
  })

  it('nextRunIsToday compares at minute precision in the given zone', () => {
    const utc = (h: number, m: number, s = 0) => new Date(Date.UTC(2026, 8, 29, h, m, s))
    expect(nextRunIsToday('09:00', 'UTC', utc(8, 59, 59))).toBe(true)
    expect(nextRunIsToday('09:00', 'UTC', utc(9, 0, 0))).toBe(false)
    expect(nextRunIsToday('09:00', 'UTC', utc(9, 0, 30))).toBe(false)
    expect(nextRunIsToday('00:00', 'UTC', utc(0, 0))).toBe(false)
    expect(nextRunIsToday('23:59', 'UTC', utc(23, 58))).toBe(true)
    // 08:30 UTC is 16:30 in Shanghai: 09:00 there has passed, 17:00 has not.
    expect(nextRunIsToday('09:00', 'Asia/Shanghai', utc(8, 30))).toBe(false)
    expect(nextRunIsToday('17:00', 'Asia/Shanghai', utc(8, 30))).toBe(true)
    expect(nextRunIsToday('', 'UTC', utc(0, 0))).toBe(false)
  })

  it('builtFromOptions puts the built-in first and drops private copies and kirocrew-lite', () => {
    expect(
      builtFromOptions([
        { name: 'zeta' },
        { name: 'kirocrew' },
        { name: 'kirocrew-lite' },
        { name: 'alpha', private_to: 'someone' },
        { name: 'beta' },
      ]),
    ).toEqual(['kirocrew', 'beta', 'zeta'])
    expect(builtFromOptions(undefined)).toEqual(['kirocrew'])
  })

  it('treats a default-only roster as empty; Mate, the first crewmate, is a crewmate', () => {
    expect(hasNoCrewmates([])).toBe(true)
    expect(hasNoCrewmates([{ name: 'default' }])).toBe(true)
    expect(hasNoCrewmates([{ name: 'default' }, { name: 'mate' }])).toBe(false)
    expect(hasNoCrewmates([{ name: 'default' }, { name: 'Radar' }])).toBe(false)
    expect(hasNoCrewmates(undefined)).toBe(false)
  })
})
