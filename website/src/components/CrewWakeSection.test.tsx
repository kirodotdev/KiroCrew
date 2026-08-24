import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

/**
 * Tests for the crew editor's "what wakes this crew" section.
 *
 * The attribution rule is the part worth pinning: a cron carries the crew it
 * runs as in `agent`, and an EMPTY `agent` means the default crew — so the
 * default crew's section must claim those or they would appear under no crew at
 * all. Both directions are asserted, because getting the empty case wrong is
 * silent (a job simply vanishes from every crew).
 */

const H = vi.hoisted(() => ({
  crons: vi.fn(),
  toggleCron: vi.fn(),
  runCron: vi.fn(),
  navigate: vi.fn(),
  createCron: vi.fn(),
  models: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    crons: H.crons,
    toggleCron: H.toggleCron,
    runCron: H.runCron,
    cancelCron: vi.fn(),
    cronToChat: vi.fn(),
    createCron: H.createCron,
    updateCron: vi.fn(),
    models: H.models,
  },
}))

vi.mock('react-router-dom', () => ({ useNavigate: () => H.navigate }))

import CrewWakeSection from './CrewWakeSection'

function wrap(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>)
}

const JOB = {
  id: 'j1', name: 'gh-autofix-dispatcher', message: 'go', enabled: true,
  schedule: 'every 15m', last_status: 'ok', agent: 'kirocrew-autofix',
  last_run_ts: Math.floor(Date.now() / 1000) - 240,
  next_run_ts: Math.floor(Date.now() / 1000) + 660,
}

beforeEach(() => {
  H.crons.mockReset(); H.toggleCron.mockReset(); H.runCron.mockReset(); H.navigate.mockReset()
  H.createCron.mockReset(); H.models.mockReset()
  H.toggleCron.mockResolvedValue({})
  H.runCron.mockResolvedValue({})
  H.createCron.mockResolvedValue({})
  H.models.mockResolvedValue([])
})
afterEach(cleanup)

describe('CrewWakeSection', () => {
  it('lists a cron bound to this crew', async () => {
    H.crons.mockResolvedValue({ jobs: [JOB] })
    wrap(<CrewWakeSection crew="kirocrew-autofix" isDefaultCrew={false} />)
    expect(await screen.findByText('gh-autofix-dispatcher')).toBeTruthy()
    expect(screen.getByText('every 15m')).toBeTruthy()
    expect(screen.getAllByTestId('wake-row')).toHaveLength(1)
  })

  it('shows the empty state when nothing is bound to this crew', async () => {
    H.crons.mockResolvedValue({ jobs: [JOB] })
    wrap(<CrewWakeSection crew="kirocrew-research" isDefaultCrew={false} />)
    expect(await screen.findByText(/No schedules run this agent automatically/i)).toBeTruthy()
    expect(screen.queryAllByTestId('wake-row')).toHaveLength(0)
  })

  it("claims an agent-less cron for the default crew only", async () => {
    H.crons.mockResolvedValue({ jobs: [{ ...JOB, id: 'j2', name: 'start a day', agent: '' }] })
    wrap(<CrewWakeSection crew="default" isDefaultCrew />)
    expect(await screen.findByText('start a day')).toBeTruthy()

    cleanup()
    wrap(<CrewWakeSection crew="kirocrew-autofix" isDefaultCrew={false} />)
    await screen.findByText(/No schedules run this agent automatically/i)
    expect(screen.queryAllByTestId('wake-row')).toHaveLength(0)
  })

  it('pauses a running job through the cron API', async () => {
    H.crons.mockResolvedValue({ jobs: [JOB] })
    wrap(<CrewWakeSection crew="kirocrew-autofix" isDefaultCrew={false} />)
    fireEvent.click(await screen.findByLabelText(/Pause gh-autofix-dispatcher/))
    await waitFor(() => expect(H.toggleCron).toHaveBeenCalledWith('j1', false))
  })

  it('resumes a paused job and reports it as paused', async () => {
    H.crons.mockResolvedValue({ jobs: [{ ...JOB, enabled: false, next_run_ts: null }] })
    wrap(<CrewWakeSection crew="kirocrew-autofix" isDefaultCrew={false} />)
    expect(await screen.findByText('paused')).toBeTruthy()
    fireEvent.click(screen.getByLabelText(/Resume gh-autofix-dispatcher/))
    await waitFor(() => expect(H.toggleCron).toHaveBeenCalledWith('j1', true))
  })

  it('runs a job now', async () => {
    H.crons.mockResolvedValue({ jobs: [JOB] })
    wrap(<CrewWakeSection crew="kirocrew-autofix" isDefaultCrew={false} />)
    fireEvent.click(await screen.findByLabelText(/Run gh-autofix-dispatcher now/))
    await waitFor(() => expect(H.runCron).toHaveBeenCalledWith('j1'))
  })

  it('refuses to run a paused job, matching the Schedule page', async () => {
    H.crons.mockResolvedValue({ jobs: [{ ...JOB, enabled: false, next_run_ts: null }] })
    wrap(<CrewWakeSection crew="kirocrew-autofix" isDefaultCrew={false} />)
    const run = await screen.findByLabelText(/Run gh-autofix-dispatcher now/)
    expect((run as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(run)
    expect(H.runCron).not.toHaveBeenCalled()
  })

  it('sends the reader to the Schedule page, which owns creation and editing', async () => {
    H.crons.mockResolvedValue({ jobs: [JOB] })
    wrap(<CrewWakeSection crew="kirocrew-autofix" isDefaultCrew={false} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open Schedule' }))
    expect(H.navigate).toHaveBeenCalledWith('/schedule')
  })

  // A script or command cron opens no session, so it runs as no crew at all and
  // an empty `agent` on it must not be read as "the default crew".
  it('does not let the default crew claim script or command crons', async () => {
    H.crons.mockResolvedValue({ jobs: [
      { ...JOB, id: 's1', name: 'nightly-cleanup', agent: '', command: 'echo hi' },
      { ...JOB, id: 's2', name: 'poller', agent: '', script: '~/.kiro/crew/crons/p.py:run' },
    ] })
    wrap(<CrewWakeSection crew="default" isDefaultCrew />)
    expect(await screen.findByText(/No schedules run this agent automatically/i)).toBeTruthy()
    expect(screen.queryAllByTestId('wake-row')).toHaveLength(0)
  })

  it('surfaces a failed pause instead of swallowing it', async () => {
    H.crons.mockResolvedValue({ jobs: [JOB] })
    H.toggleCron.mockResolvedValue({ error: 'cron store busy, please retry' })
    wrap(<CrewWakeSection crew="kirocrew-autofix" isDefaultCrew={false} />)
    fireEvent.click(await screen.findByLabelText(/Pause gh-autofix-dispatcher/))
    expect(await screen.findByRole('alert')).toHaveTextContent(/cron store busy/)
  })

  // `agent_sequence` wins over `agent` at run time, so the crews it names own the
  // job and an empty `agent` on it must not read as "the default crew".
  it('attributes a sequence job to the crews it names, not to the default crew', async () => {
    const seq = { ...JOB, id: 'q1', name: 'nightly-chain', agent: '', agent_sequence: ['ops-triage', 'kirocrew-autofix'] }
    H.crons.mockResolvedValue({ jobs: [seq] })
    wrap(<CrewWakeSection crew="kirocrew-autofix" isDefaultCrew={false} />)
    expect(await screen.findByText('nightly-chain')).toBeTruthy()

    cleanup()
    wrap(<CrewWakeSection crew="default" isDefaultCrew />)
    expect(await screen.findByText(/No schedules run this agent automatically/i)).toBeTruthy()
  })

  // The gateway takes the sequence path only at `len(agents) > 1`, so a
  // one-element sequence resolves through `agent_id` like any other job.
  it('resolves a one-element sequence through the bound agent, not the sequence', async () => {
    const one = { ...JOB, id: 'q2', name: 'single-chain', agent: 'kirocrew-autofix', agent_sequence: ['ops-triage'] }
    H.crons.mockResolvedValue({ jobs: [one] })
    wrap(<CrewWakeSection crew="kirocrew-autofix" isDefaultCrew={false} />)
    expect(await screen.findByText('single-chain')).toBeTruthy()

    cleanup()
    wrap(<CrewWakeSection crew="ops-triage" isDefaultCrew={false} />)
    expect(await screen.findByText(/No schedules run this agent automatically/i)).toBeTruthy()
  })

  it('never lists a script job, even when it carries a stale agent', async () => {
    H.crons.mockResolvedValue({ jobs: [
      { ...JOB, id: 'x1', name: 'stale-script', script: '~/.kiro/crew/crons/p.py:run' },
    ] })
    wrap(<CrewWakeSection crew="kirocrew-autofix" isDefaultCrew={false} />)
    expect(await screen.findByText(/No schedules run this agent automatically/i)).toBeTruthy()
  })

  // Absence of an answer and an answer of "none" must not render the same.
  it('says the answer is unknown when the fetch fails, not that nothing wakes it', async () => {
    H.crons.mockRejectedValue(new Error('gateway down'))
    wrap(<CrewWakeSection crew="kirocrew-autofix" isDefaultCrew={false} />)
    expect(await screen.findByRole('alert')).toHaveTextContent(/what wakes it is unknown/i)
    expect(screen.queryByText(/No schedules run this agent automatically/i)).toBeNull()
  })

  it('reports a job that is running right now', async () => {
    H.crons.mockResolvedValue({ jobs: [{ ...JOB, is_running: true }] })
    wrap(<CrewWakeSection crew="kirocrew-autofix" isDefaultCrew={false} />)
    expect(await screen.findByText('running')).toBeTruthy()
    expect((await screen.findByLabelText(/Run gh-autofix-dispatcher now/) as HTMLButtonElement).disabled).toBe(true)
  })

  it('surfaces a thrown pause failure too', async () => {
    H.crons.mockResolvedValue({ jobs: [JOB] })
    H.toggleCron.mockRejectedValue(new Error('network down'))
    wrap(<CrewWakeSection crew="kirocrew-autofix" isDefaultCrew={false} />)
    fireEvent.click(await screen.findByLabelText(/Pause gh-autofix-dispatcher/))
    expect(await screen.findByRole('alert')).toHaveTextContent(/network down/)
  })
})

describe('CrewWakeSection — inline schedule creation', () => {
  async function openForm(crew = 'kirocrew-autofix') {
    H.crons.mockResolvedValue({ jobs: [] })
    wrap(<CrewWakeSection crew={crew} isDefaultCrew={false} />)
    fireEvent.click(await screen.findByTestId('crew-wake-add'))
    return screen.getByTestId('crew-wake-create')
  }

  it('expands an inline create form pinned to THIS crew', async () => {
    await openForm('kirocrew-autofix')
    // The crew is a rendered fact, not a picker: filing the job on another
    // crew from inside this crew's editor would be the mistake, not a choice.
    const chip = screen.getByTestId('jobform-locked-agent')
    expect(chip.textContent).toBe('kirocrew-autofix')
    expect(screen.queryByRole('combobox', { name: 'Agent' })).toBeNull()
    // A long crew name must wrap inside the pane instead of running past its
    // clipped edge at 320px: the chip is width-bounded and breaks anywhere,
    // because identifier-like names have no natural break points.
    expect(chip.className).toContain('max-w-full')
    expect(chip.className).toContain('break-all')
  })

  it('creates the job carrying this crew as its agent', async () => {
    await openForm('kirocrew-autofix')
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'morning digest' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'summarize open work' } })
    fireEvent.click(screen.getByRole('button', { name: /Create/ }))
    await waitFor(() => expect(H.createCron).toHaveBeenCalledTimes(1))
    const body = H.createCron.mock.calls[0][0]
    expect(body.agent).toBe('kirocrew-autofix')
    expect(body.name).toBe('morning digest')
    expect(body.message).toBe('summarize open work')
  })

  it('collapses the form and refreshes the list after a save', async () => {
    await openForm()
    const callsBefore = H.crons.mock.calls.length
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'n' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'm' } })
    fireEvent.click(screen.getByRole('button', { name: /Create/ }))
    // The saved job's evidence is the refreshed list, not a lingering form.
    await waitFor(() => expect(screen.queryByTestId('crew-wake-create')).toBeNull())
    expect(H.crons.mock.calls.length).toBeGreaterThan(callsBefore)
  })

  it('the toggle collapses an open form without saving anything', async () => {
    await openForm()
    fireEvent.click(screen.getByTestId('crew-wake-add'))
    expect(screen.queryByTestId('crew-wake-create')).toBeNull()
    expect(H.createCron).not.toHaveBeenCalled()
  })
})

describe('CrewWakeSection — draft accounting and the visible Create', () => {
  it('submits through the always-visible header Create button', async () => {
    H.crons.mockResolvedValue({ jobs: [] })
    wrap(<CrewWakeSection crew="oncall" isDefaultCrew={false} />)
    fireEvent.click(await screen.findByTestId('crew-wake-add'))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'n' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'm' } })
    // The header button, not JobForm's own below-the-fold submit.
    fireEvent.click(screen.getByTestId('crew-wake-create-submit'))
    await waitFor(() => expect(H.createCron).toHaveBeenCalledTimes(1))
    expect(H.createCron.mock.calls[0][0].agent).toBe('oncall')
  })

  it('reports TYPED work, not open-ness — and clears on close and unmount', async () => {
    H.crons.mockResolvedValue({ jobs: [] })
    const onDraftChange = vi.fn()
    const { unmount } = wrap(
      <CrewWakeSection crew="oncall" isDefaultCrew={false} onDraftChange={onDraftChange} />,
    )
    // Opening the form is not work: whoever opens it to look and backs out
    // must never meet a "what you typed will be lost" confirm.
    fireEvent.click(await screen.findByTestId('crew-wake-add'))
    expect(onDraftChange).not.toHaveBeenCalledWith(true)
    // Typing is work; typing it back away un-works it.
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'n' } })
    expect(onDraftChange).toHaveBeenLastCalledWith(true)
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: '' } })
    expect(onDraftChange).toHaveBeenLastCalledWith(false)
    // The toggle collapses a dirty form: the accounting clears with it (a
    // stale true would leave the host's Save disabled with nothing to finish).
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'n2' } })
    expect(onDraftChange).toHaveBeenLastCalledWith(true)
    fireEvent.click(screen.getByTestId('crew-wake-add'))
    expect(onDraftChange).toHaveBeenLastCalledWith(false)
    // Unmount (a pane switch) with typed work: same clearing rule.
    fireEvent.click(screen.getByTestId('crew-wake-add'))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'n3' } })
    expect(onDraftChange).toHaveBeenLastCalledWith(true)
    unmount()
    expect(onDraftChange).toHaveBeenLastCalledWith(false)
  })

  it('hides the Schedule-page jump while the form is open — it would discard the draft', async () => {
    H.crons.mockResolvedValue({ jobs: [] })
    wrap(<CrewWakeSection crew="oncall" isDefaultCrew={false} />)
    expect(await screen.findByText('Open Schedule')).toBeTruthy()
    fireEvent.click(screen.getByTestId('crew-wake-add'))
    expect(screen.queryByText('Open Schedule')).toBeNull()
  })

  it('names WHICH cancel this is for assistive tech', async () => {
    // The dialog footer's own Cancel (close the whole editor) can be on screen
    // at the same time; two controls announced identically with different
    // blast radii is the trap this name exists to avoid.
    H.crons.mockResolvedValue({ jobs: [] })
    wrap(<CrewWakeSection crew="oncall" isDefaultCrew={false} />)
    fireEvent.click(await screen.findByTestId('crew-wake-add'))
    expect(screen.getByRole('button', { name: 'Cancel new schedule' })).toBeTruthy()
  })
})

describe('CrewWakeSection — a second create works after the first', () => {
  it('re-enables the header Create after a successful save collapses the form', async () => {
    // The form unmounts on save before it can report saving=false; the
    // section must clear the flag itself or the SECOND create in the same
    // pane visit renders a permanently disabled "Saving…" button.
    H.crons.mockResolvedValue({ jobs: [] })
    wrap(<CrewWakeSection crew="oncall" isDefaultCrew={false} />)
    fireEvent.click(await screen.findByTestId('crew-wake-add'))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'first' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'm' } })
    fireEvent.click(screen.getByTestId('crew-wake-create-submit'))
    await waitFor(() => expect(screen.queryByTestId('crew-wake-create')).toBeNull())

    fireEvent.click(screen.getByTestId('crew-wake-add'))
    const again = screen.getByTestId('crew-wake-create-submit')
    expect(again).not.toBeDisabled()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'second' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'm2' } })
    fireEvent.click(again)
    await waitFor(() => expect(H.createCron).toHaveBeenCalledTimes(2))
    expect(H.createCron.mock.calls[1][0].name).toBe('second')
  })
})

describe('CrewWakeSection — a discard cannot outrun an in-flight create', () => {
  it('locks the cancel toggle while the create request is in flight', async () => {
    // Cancelling mid-flight unmounts the form but does NOT cancel the POST:
    // the schedule the user watched being "discarded" would persist. The
    // toggle stays locked until the request settles, then the collapse path
    // takes over.
    let resolveCreate!: (v: unknown) => void
    H.createCron.mockReturnValue(new Promise(res => { resolveCreate = res }))
    H.crons.mockResolvedValue({ jobs: [] })
    wrap(<CrewWakeSection crew="oncall" isDefaultCrew={false} />)
    fireEvent.click(await screen.findByTestId('crew-wake-add'))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'n' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'm' } })
    fireEvent.click(screen.getByTestId('crew-wake-create-submit'))
    const toggle = screen.getByTestId('crew-wake-add')
    await waitFor(() => expect(toggle).toBeDisabled())
    // Clicking the locked toggle destroys nothing.
    fireEvent.click(toggle)
    expect(screen.getByTestId('crew-wake-create')).toBeTruthy()
    resolveCreate({})
    await waitFor(() => expect(screen.queryByTestId('crew-wake-create')).toBeNull())
    expect(screen.getByTestId('crew-wake-add')).not.toBeDisabled()
  })

  it('reports the in-flight state up so the host can lock its discard paths', async () => {
    let resolveCreate!: (v: unknown) => void
    H.createCron.mockReturnValue(new Promise(res => { resolveCreate = res }))
    H.crons.mockResolvedValue({ jobs: [] })
    const onSavingChange = vi.fn()
    wrap(<CrewWakeSection crew="oncall" isDefaultCrew={false} onSavingChange={onSavingChange} />)
    fireEvent.click(await screen.findByTestId('crew-wake-add'))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'n' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'm' } })
    fireEvent.click(screen.getByTestId('crew-wake-create-submit'))
    await waitFor(() => expect(onSavingChange).toHaveBeenLastCalledWith(true))
    resolveCreate({})
    await waitFor(() => expect(onSavingChange).toHaveBeenLastCalledWith(false))
  })
})

describe('CrewWakeSection — the header survives a 216px pane', () => {
  it('both actions keep their full names when the text collapses to icon-only', async () => {
    // Below `md` the visible words hide (the pane runs as narrow as ~216px
    // and two intrinsic-width buttons overflow its clipped header), so the
    // full name must ride aria-label — otherwise the collapse would rename
    // both controls to nothing. The class pins the collapse mechanism; the
    // role queries pin that the names survive it.
    H.crons.mockResolvedValue({ jobs: [] })
    wrap(<CrewWakeSection crew="oncall" isDefaultCrew={false} />)
    const add = await screen.findByRole('button', { name: 'New schedule' })
    const open = screen.getByRole('button', { name: 'Open Schedule' })
    for (const btn of [add, open]) {
      const label = btn.querySelector('span.hidden.md\\:inline')
      expect(label, 'visible text must collapse below md').toBeTruthy()
    }
    fireEvent.click(add)
    expect(screen.getByRole('button', { name: 'Cancel new schedule' })).toBeTruthy()
  })

  it('a failed submit scrolls the below-the-fold error notice into view', async () => {
    // Create lives in the card header while the notice renders at the form's
    // BOTTOM — on a pane shorter than the form, a validation failure would
    // otherwise happen entirely off-screen and read as a dead button.
    const scrolled = vi.fn()
    Element.prototype.scrollIntoView = scrolled
    H.crons.mockResolvedValue({ jobs: [] })
    wrap(<CrewWakeSection crew="oncall" isDefaultCrew={false} />)
    fireEvent.click(await screen.findByTestId('crew-wake-add'))
    fireEvent.click(screen.getByTestId('crew-wake-create-submit'))
    expect(await screen.findByText('Name is required')).toBeTruthy()
    expect(scrolled).toHaveBeenCalled()
    // A REPEATED identical failure scrolls again: the user may have scrolled
    // back up, and batching must not swallow the second signal.
    scrolled.mockClear()
    fireEvent.click(screen.getByTestId('crew-wake-create-submit'))
    await waitFor(() => expect(scrolled).toHaveBeenCalled())
  })
})
