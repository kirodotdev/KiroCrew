import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), crewTimeline: vi.fn() }))
vi.mock('../../../api/client', () => ({ api }))
const panel = vi.hoisted(() => ({ openPanelView: vi.fn() }))
vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: panel.openPanelView }))
const motion = vi.hoisted(() => ({ reduced: false }))
vi.mock('../../../hooks/useReducedMotion', () => ({ useReducedMotion: () => motion.reduced }))
const mobile = vi.hoisted(() => ({ value: false }))
vi.mock('../../../hooks/useIsMobile', () => ({ useIsMobile: () => mobile.value }))

import CrewTimeline from './CrewTimeline'
import CrewWorkerList from './CrewWorkerList'

const at = (s: number) => new Date(Date.UTC(2026, 9, 21, 3, 0, s)).toISOString()

const lane = (key: string, extra: Record<string, unknown> = {}) => ({
  key,
  title: key,
  agent: 'kirocrew-worker',
  model: 'claude-test',
  model_source: 'served',
  role: 'worker',
  status: 'idle',
  group: 'done',
  running: false,
  waiting: false,
  segments: [],
  marks: [],
  credits: 0,
  first_ts: at(0),
  last_ts: at(60),
  dropped_segments: 0,
  ...extra,
})

const DONE = {
  root: 'root-1',
  now: at(90),
  live: false,
  started: at(0),
  ended: at(60),
  elapsed_ms: 60_000,
  credits: 1.25,
  lanes: [
    lane('root-1', {
      title: 'Welcome kit',
      role: 'conductor',
      model: 'claude-conductor',
      segments: [{ kind: 'turn', start: at(0), end: at(30), end_bound: 'recorded', label: '' }],
    }),
    lane('w-1', {
      title: 'Welcome note',
      segments: [
        { kind: 'tool', start: at(10), end: at(20), end_bound: 'next_row', label: '@x/work_brief' },
        { kind: 'waiting', start: at(20), end: at(30), end_bound: 'recorded', label: '' },
      ],
      marks: [{ kind: 'failed', ts: at(40), label: 'boom' }],
    }),
    lane('w-gone', { title: '', model: '', model_source: 'default', status: 'closed', first_ts: '', last_ts: '' }),
    lane('w-auto', { title: 'A very long worker title that needs two lines', model: 'auto', model_source: 'default', first_ts: '', last_ts: '' }),
  ],
  handoffs: [{ from: 'root-1', to: 'w-1', start: at(5), end: at(6) }],
  gaps: ['tool_end_time', 'tokens', 'some_future_gap'],
}

function renderWith(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots: [] } } as never)
  render(<QueryClientProvider client={qc}><Provider store={store}>{ui}</Provider></QueryClientProvider>)
  return store
}

describe('CrewTimeline', () => {
  afterEach(() => {
    // A test that times out inside a faked section never reaches its finally,
    // so the clock is restored here as well, or the next tests run on it.
    vi.useRealTimers()
    vi.clearAllMocks()
    motion.reduced = false
    mobile.value = false
    localStorage.removeItem('mc-crew-workers-finished-open')
  })

  it('keeps a pixel gap between axis labels on a narrow, long-running timeline', async () => {
    const original = HTMLElement.prototype.getBoundingClientRect
    const boxes = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      if (!this.hasAttribute('data-crew-axis-tick')) return original.call(this)
      const left = parseFloat(this.style.left) * 2
      return { x: left, y: 0, left, right: left + 48, top: 0, bottom: 12, width: 48, height: 12, toJSON: () => ({}) } as DOMRect
    })
    try {
      api.crewTimeline.mockResolvedValue({ ...DONE, ended: at(250_000) })
      renderWith(<CrewTimeline root="root-1" viewing="root-1" active onOpen={vi.fn()} />)
      const timeline = await screen.findByTestId('crew-timeline')
      const labels = Array.from(timeline.querySelectorAll<HTMLElement>('[data-crew-axis-tick]'))
      const visible = labels.filter(label => !label.classList.contains('invisible'))
      expect(visible.length).toBeGreaterThan(1)
      expect(visible.length).toBeLessThan(labels.length)
      expect(visible[0]).toHaveTextContent('0s')
      for (let i = 1; i < visible.length; i++) {
        expect(visible[i].getBoundingClientRect().left - visible[i - 1].getBoundingClientRect().right).toBeGreaterThanOrEqual(6)
      }
    } finally {
      cleanup()
      boxes.mockRestore()
    }
  })

  it('draws a lane per session, conductor first, each with its model', async () => {
    api.crewTimeline.mockResolvedValue(DONE)
    renderWith(<CrewTimeline root="root-1" viewing="root-1" active onOpen={vi.fn()} />)
    const lanes = await screen.findAllByTestId('crew-timeline-lane')
    expect(lanes.map(l => l.getAttribute('data-lane'))).toEqual(['root-1', 'w-1', 'w-gone', 'w-auto'])
    expect(lanes[0]).toHaveAttribute('data-role', 'conductor')
    expect(within(lanes[0]).getByText('Conductor')).toBeInTheDocument()
    expect(within(lanes[0]).getByTestId('crew-timeline-model')).toHaveTextContent('claude-conductor')
    expect(within(lanes[2]).getByTestId('crew-timeline-model')).toHaveTextContent('Default model')
    // The gateway default, named when it is known; the full name is the tooltip.
    expect(within(lanes[3]).getByTestId('crew-timeline-model')).toHaveTextContent('Default (auto)')
    expect(within(lanes[3]).getByTestId('crew-timeline-title')).toHaveClass('line-clamp-2')
    expect(within(lanes[3]).getByTestId('crew-timeline-title')).not.toHaveClass('block')
    expect(within(lanes[3]).getByTitle('A very long worker title that needs two lines · Default (auto)')).toBeInTheDocument()
  })

  it('places bars, hand-offs and marks only from the reply', async () => {
    api.crewTimeline.mockResolvedValue(DONE)
    renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
    const worker = (await screen.findAllByTestId('crew-timeline-lane'))[1]
    const bars = within(worker).getAllByTestId('crew-timeline-bar')
    expect(bars.map(b => b.getAttribute('data-kind'))).toEqual(['tool', 'waiting'])
    // 10s..20s of a 60s window.
    expect(bars[0].style.getPropertyValue('--crew-bar-left')).toBe(`${(10 / 60) * 100}%`)
    expect(bars[0]).toHaveClass('left-[min(var(--crew-bar-left),calc(100%_-_6px))]')
    expect(bars[0]).toHaveAttribute('data-end-bound', 'next_row')
    // A hand-off is one arrow from the conductor's lane to the worker's.
    const arrows = screen.getAllByTestId('crew-timeline-handoff')
    expect(arrows).toHaveLength(1)
    expect(arrows[0]).toHaveAttribute('data-from', 'root-1')
    expect(arrows[0]).toHaveAttribute('data-to', 'w-1')
    expect(bars[0]).toHaveAttribute('data-width-pct', ((10 / 60) * 100).toFixed(2))
    expect(within(worker).getByTestId('crew-timeline-mark')).toHaveAttribute('data-kind', 'failed')
    // The closed lane draws nothing.
    const gone = screen.getAllByTestId('crew-timeline-lane')[2]
    expect(within(gone).queryAllByTestId('crew-timeline-bar')).toHaveLength(0)
  })

  it('draws a closed worker from its saved transcript under its saved title', async () => {
    const saved = lane('w-gone', {
      title: 'Write the welcome note', status: 'closed', record: true,
      segments: [{ kind: 'turn', start: at(40), end: at(50), end_bound: 'recorded', label: '' }],
    })
    api.crewTimeline.mockResolvedValue({ ...DONE, lanes: [DONE.lanes[0], saved] })
    renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
    const gone = (await screen.findAllByTestId('crew-timeline-lane'))[1]
    expect(within(gone).getByTestId('crew-timeline-title')).toHaveTextContent('Write the welcome note')
    expect(within(gone).getAllByTestId('crew-timeline-bar')).toHaveLength(1)
    expect(within(gone).queryByTestId('crew-timeline-no-record')).toBeNull()
  })

  it('says a closed worker with no saved record on one muted line, not an empty track', async () => {
    const lost = lane('w-gone', { title: 'Pick a lunch place', status: 'closed', record: false, first_ts: '', last_ts: '' })
    api.crewTimeline.mockResolvedValue({ ...DONE, lanes: [DONE.lanes[0], lost], gaps: ['closed_worker_rows'] })
    renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
    const gone = (await screen.findAllByTestId('crew-timeline-lane'))[1]
    expect(within(gone).getByTestId('crew-timeline-title')).toHaveTextContent('Pick a lunch place')
    expect(within(gone).getByTestId('crew-timeline-no-record')).toHaveTextContent('Closed · no record')
    expect(gone.querySelector('[data-track-of]')).toBeNull()
    const tip = (await screen.findByTestId('crew-timeline-gaps')).querySelector('[title]')?.getAttribute('title') ?? ''
    expect(tip).toContain('Closed crew members whose saved transcript can no longer be read')
  })

  it('heads the view with real elapsed time and credits, and is static once done', async () => {
    api.crewTimeline.mockResolvedValue(DONE)
    renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
    expect(await screen.findByTestId('crew-timeline-credits')).toHaveTextContent('1.25 credits')
    expect(screen.getByTestId('crew-timeline-elapsed')).toHaveTextContent('Elapsed')
    expect(screen.getByText('Finished')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-timeline-playhead')).toBeNull()
  })

  it('says Finished only when no worker is open: an open worker names its group', async () => {
    const withGroup = (group: string) => ({ ...DONE, lanes: DONE.lanes.map(l => (l.key === 'w-1' ? { ...l, group } : l)) })
    api.crewTimeline.mockResolvedValue(withGroup('waiting'))
    renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
    expect(await screen.findByTestId('crew-timeline-header')).toHaveTextContent('Waiting')
    expect(screen.queryByText('Finished')).toBeNull()
    cleanup()
    api.crewTimeline.mockResolvedValue(withGroup('approval'))
    renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
    expect(await screen.findByTestId('crew-timeline-header')).toHaveTextContent('Approval needed')
    cleanup()
    api.crewTimeline.mockResolvedValue(withGroup('with_conductor'))
    renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
    expect(await screen.findByTestId('crew-timeline-header')).toHaveTextContent('Waiting on conductor')
  })

  it('says Not started, with no elapsed time or spend, before anything is recorded (X7)', async () => {
    api.crewTimeline.mockResolvedValue({ ...DONE, lanes: DONE.lanes.map(l => ({ ...l, segments: [], marks: [] })) })
    renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
    expect(await screen.findByTestId('crew-timeline-header')).toHaveTextContent('Not started')
    expect(screen.queryByText('Finished')).toBeNull()
    expect(screen.queryByTestId('crew-timeline-elapsed')).toBeNull()
    expect(screen.queryByTestId('crew-timeline-credits')).toBeNull()
  })

  it('draws a lane for every worker the List shows, open or closed, even with nothing recorded', async () => {
    // Rows with no usable time (the pod's chat-9 root): no lane has a bar, and
    // `started` is null. The List still shows the workers, so the Timeline must too.
    const workers = [
      { key: 'w-1', title: 'Waiting one', agent: 'kirocrew-worker', status: 'idle', group: 'waiting', last_activity: '', ledger_item: '', summary: '', created_by: '' },
      { key: 'w-gone', title: 'Done one', agent: 'kirocrew-worker', status: 'closed', group: 'done', last_activity: '', ledger_item: '', summary: '', created_by: '' },
    ]
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers })
    api.crewTimeline.mockResolvedValue({
      ...DONE, started: null, ended: null, handoffs: [],
      lanes: [lane('root-1', { role: 'conductor' }), lane('w-1', { group: 'waiting' }), lane('w-gone', { status: 'closed' })],
    })
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    // The List folds Done away by default: open it so it shows every worker.
    fireEvent.click(await screen.findByTestId('crew-finished-toggle'))
    const listed = (await screen.findAllByRole('button', { name: /Waiting one|Done one/ })).length
    fireEvent.click(screen.getByRole('button', { name: 'Timeline' }))
    const lanes = await screen.findAllByTestId('crew-timeline-lane')
    const workerLanes = lanes.filter(l => l.getAttribute('data-role') === 'worker').map(l => l.getAttribute('data-lane'))
    expect(new Set(workerLanes)).toEqual(new Set(workers.map(w => w.key)))
    expect(workerLanes).toHaveLength(listed)
    expect(screen.getByText('Nothing recorded yet.')).toBeInTheDocument()
  })

  it('shows a loading line while the first read is in flight (X6)', () => {
    api.crewTimeline.mockReturnValue(new Promise(() => {}))
    renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
    expect(screen.getByTestId('crew-timeline-loading')).toHaveTextContent('Loading the timeline…')
  })

  it('advances a live playhead between reads when motion is allowed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      api.crewTimeline.mockResolvedValue({ ...DONE, live: true })
      renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
      const before = (await screen.findByTestId('crew-timeline-playhead')).style.left
      await act(async () => { vi.advanceTimersByTime(1000) })
      expect(screen.getByTestId('crew-timeline-playhead').style.left).not.toBe(before)
    } finally {
      vi.useRealTimers()
    }
  })

  it('draws no playhead under reduced motion, and still says Live', async () => {
    motion.reduced = true
    api.crewTimeline.mockResolvedValue({ ...DONE, live: true, lanes: DONE.lanes.map(l => ({ ...l })) })
    renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
    expect(await screen.findByText('Live')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-timeline-playhead')).toBeNull()
  })

  it('holds still while the person types in the composer', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      api.crewTimeline.mockResolvedValue({ ...DONE, live: true })
      renderWith(<><div className="input-area"><textarea aria-label="Message" data-testid="composer" /></div><CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} /></>)
      const head = await screen.findByTestId('crew-timeline-playhead')
      fireEvent.input(screen.getByTestId('composer'), { target: { value: 'h' } })
      const before = head.style.left
      await act(async () => { vi.advanceTimersByTime(1000) })
      expect(screen.getByTestId('crew-timeline-playhead').style.left).toBe(before)
      // A moment after the last keystroke it moves again.
      await act(async () => { vi.advanceTimersByTime(2000) })
      await act(async () => { vi.advanceTimersByTime(1000) })
      expect(screen.getByTestId('crew-timeline-playhead').style.left).not.toBe(before)
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps the gaps behind one (i) beside the legend, not raw codes', async () => {
    api.crewTimeline.mockResolvedValue(DONE)
    renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
    const gaps = await screen.findByTestId('crew-timeline-gaps')
    const tip = gaps.querySelector('[title]')?.getAttribute('title') ?? ''
    expect(tip).toContain('Tokens per turn')
    expect(tip).not.toContain('some_future_gap')
    // Nothing of it is body copy until the tip opens.
    expect(screen.getByTestId('crew-timeline').textContent).not.toContain('Tokens per turn')
  })

  it('says the scale is compressed when a long stretch is drawn shorter', async () => {
    const long = { ...DONE, ended: at(0).replace('03:00:00', '03:40:00'), lanes: DONE.lanes.map(l => ({ ...l })) }
    long.lanes[1] = { ...long.lanes[1], segments: [
      { kind: 'tool', start: at(10), end: at(20), end_bound: 'next_row', label: '' },
      { kind: 'waiting', start: at(20), end: long.ended, end_bound: 'recorded', label: '' },
    ] }
    api.crewTimeline.mockResolvedValue(long)
    renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
    const gaps = await screen.findByTestId('crew-timeline-gaps')
    expect(gaps.querySelector('[title]')?.getAttribute('title')).toContain('drawn shorter')
    // The 10s tool call is no sliver next to a 40m wait.
    const tool = screen.getAllByTestId('crew-timeline-bar').find(b => b.getAttribute('data-kind') === 'tool')!
    // True scale would give it 0.4% of the track; drawn it is several times that.
    expect(Number(tool.getAttribute('data-width-pct'))).toBeGreaterThan(2)
    // The axis itself shows where it bends: one break mark over the 40m wait.
    const breaks = screen.getAllByTestId('crew-timeline-break')
    expect(breaks).toHaveLength(1)
    expect(breaks[0].getAttribute('title')).toContain('drawn shorter')
    expect(breaks[0].textContent).toBe('\u2AFD')
  })

  it('draws no break mark on a scale that is true, and lists only the legend kinds the run has', async () => {
    api.crewTimeline.mockResolvedValue(DONE)
    renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
    await screen.findByTestId('crew-timeline-legend')
    expect(screen.queryByTestId('crew-timeline-break')).toBeNull()
    const legend = screen.getByTestId('crew-timeline-legend').textContent ?? ''
    // DONE has a hand-off and a failed mark, so both are in the legend...
    expect(legend).toContain('Handoff')
    expect(legend).toContain('Failed')
    // ...and a run without them does not send the reader looking for them.
    const plain = { ...DONE, handoffs: [], lanes: DONE.lanes.map(l => ({ ...l, marks: [] })) }
    api.crewTimeline.mockResolvedValue(plain)
    renderWith(<CrewTimeline root="root-2" viewing={null} active onOpen={vi.fn()} />)
    await waitFor(() => expect(screen.getAllByTestId('crew-timeline-legend')).toHaveLength(2))
    const second = screen.getAllByTestId('crew-timeline-legend')[1].textContent ?? ''
    expect(second).not.toContain('Handoff')
    expect(second).not.toContain('Failed')
    expect(second).toContain('Turn')
  })

  it('opens a lane on click, and reopens a closed one (X4)', async () => {
    api.crewTimeline.mockResolvedValue(DONE)
    const onOpen = vi.fn()
    renderWith(<CrewTimeline root="root-1" viewing="w-1" active onOpen={onOpen} />)
    const button = await screen.findByRole('button', { name: 'Open Welcome note' })
    expect(button).toHaveAttribute('aria-current', 'true')
    fireEvent.click(button)
    expect(onOpen).toHaveBeenCalledWith('w-1')
    const gone = screen.getAllByTestId('crew-timeline-lane')[2]
    fireEvent.click(within(gone).getByTestId('crew-timeline-reopen'))
    expect(onOpen).toHaveBeenLastCalledWith(gone.getAttribute('data-lane'), expect.any(String), true)
  })

  it('draws a closed conductor first, under its saved title, and reopens it on click', async () => {
    // What the route answers for a CLOSED known root: its own lane is closed and
    // drawn from its saved transcript, as the worker list answers for that root.
    api.crewTimeline.mockResolvedValue({
      ...DONE,
      lanes: [
        lane('root-1', { title: 'Toy Flag Review', role: 'conductor', status: 'closed', model: '', model_source: 'default', record: true,
          segments: [{ kind: 'tool', start: at(0), end: at(10), end_bound: 'next_row', label: '@x/session_send' }] }),
        lane('w-1', { title: 'Welcome note', group: 'waiting' }),
      ],
    })
    const onOpen = vi.fn()
    renderWith(<CrewTimeline root="root-1" viewing="w-1" active onOpen={onOpen} />)
    const lanes = await screen.findAllByTestId('crew-timeline-lane')
    expect(lanes.map(l => l.getAttribute('data-lane'))).toEqual(['root-1', 'w-1'])
    expect(within(lanes[0]).getByText('Conductor')).toBeInTheDocument()
    expect(within(lanes[0]).getByTestId('crew-timeline-title')).toHaveTextContent('Toy Flag Review')
    expect(within(lanes[0]).getAllByTestId('crew-timeline-bar')).toHaveLength(1)
    fireEvent.click(within(lanes[0]).getByRole('button', { name: 'Reopen Toy Flag Review to read it' }))
    expect(onOpen).toHaveBeenCalledWith('root-1', 'Toy Flag Review', true)
    // The run is not finished: its open worker names the header.
    expect(screen.getByTestId('crew-timeline-header')).toHaveTextContent('Waiting')
  })

  it('says when it cannot be read', async () => {
    api.crewTimeline.mockRejectedValue(new Error('nope'))
    renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
    expect(await screen.findByText('Could not load the timeline.')).toBeInTheDocument()
  })

  it('says the gateway has no timeline yet, with no alert and no second read, when the route is not served', async () => {
    // The timeline route lands in a later part; an older gateway 404s. That is no
    // data, not a failure: one muted line, nothing handed to the agent, no poll.
    const { ApiError } = await import('../../../api/apiError')
    api.crewTimeline.mockRejectedValue(new ApiError(404, 'not found', ''))
    renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
    expect(await screen.findByTestId('crew-timeline-unavailable')).toHaveTextContent('This gateway has no timeline for the crew yet.')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(api.crewTimeline).toHaveBeenCalledTimes(1)
  })

  it('keeps the lanes it has when a later read fails, and says the read failed above them', async () => {
    // The server answered once; a poll then fails (a blip, a restart). The lanes
    // it returned are still what the person has: they stay, under the notice.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      api.crewTimeline.mockResolvedValueOnce({ ...DONE, live: true }).mockRejectedValue(new Error('nope'))
      renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
      expect(await screen.findAllByTestId('crew-timeline-lane')).toHaveLength(4)
      await act(async () => { vi.advanceTimersByTime(2500) })
      expect(await screen.findByText('Could not load the timeline.')).toBeInTheDocument()
      expect(api.crewTimeline).toHaveBeenCalledTimes(2)
      expect(screen.getAllByTestId('crew-timeline-lane')).toHaveLength(4)
      expect(screen.getByTestId('crew-timeline-header')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('Crew panel List | Timeline switch', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.clearAllMocks()
    mobile.value = false
    localStorage.removeItem('mc-crew-workers-finished-open')
  })

  it('starts on List and swaps to the timeline', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [] })
    api.crewTimeline.mockResolvedValue(DONE)
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    expect(screen.getByTestId('crew-view-list')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.queryByTestId('crew-timeline')).toBeNull()
    fireEvent.click(screen.getByTestId('crew-view-timeline'))
    expect(await screen.findByTestId('crew-timeline')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Open Welcome note' }))
    expect(panel.openPanelView).toHaveBeenCalledWith('w-1', 'workers')
  })

  it('stops reading the worker list while the timeline is shown', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [] })
      api.crewTimeline.mockResolvedValue(DONE)
      renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
      await act(async () => {})
      fireEvent.click(screen.getByTestId('crew-view-timeline'))
      await screen.findByTestId('crew-timeline')
      const reads = api.crewWorkers.mock.calls.length
      await act(async () => { vi.advanceTimersByTime(12_000) })
      expect(api.crewWorkers.mock.calls.length).toBe(reads)
    } finally {
      vi.useRealTimers()
    }
  })

  it('on a phone, opening a worker lands on its chat even when its sheet was left open', async () => {
    // V1: the target remembers its panel open; the switch restores that, so the
    // open must close it explicitly or the sheet covers the worker's transcript.
    mobile.value = true
    localStorage.setItem('mc-activity-open:w-1', 'true')
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [
      { key: 'w-1', title: 'Welcome note', agent: '', status: 'idle', group: 'done', last_activity: '', ledger_item: '', created_by: 'root-1' },
    ] })
    const store = renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    fireEvent.click(await screen.findByTestId('crew-finished-toggle'))
    fireEvent.click(await screen.findByRole('button', { name: /Welcome note/ }))
    expect(store.getState().chat.activeSlot).toBe('w-1')
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(localStorage.getItem('mc-activity-open:w-1')).not.toBe('true')
    localStorage.removeItem('mc-activity-open:w-1')
  })

  it('a phone gets no switch and always the list', () => {
    mobile.value = true
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [] })
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    expect(screen.queryByTestId('crew-view-switch')).toBeNull()
    expect(api.crewTimeline).not.toHaveBeenCalled()
  })
})
