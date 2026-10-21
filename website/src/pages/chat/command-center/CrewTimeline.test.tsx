import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
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
    vi.clearAllMocks()
    motion.reduced = false
    mobile.value = false
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
    expect(within(lanes[3]).getByTitle('A very long worker title that needs two lines · Default (auto)')).toBeInTheDocument()
  })

  it('places bars, hand-offs and marks only from the reply', async () => {
    api.crewTimeline.mockResolvedValue(DONE)
    renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
    const worker = (await screen.findAllByTestId('crew-timeline-lane'))[1]
    const bars = within(worker).getAllByTestId('crew-timeline-bar')
    expect(bars.map(b => b.getAttribute('data-kind'))).toEqual(['tool', 'waiting'])
    // 10s..20s of a 60s window.
    expect(bars[0].style.left).toBe(`${(10 / 60) * 100}%`)
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

  it('heads the view with real elapsed time and credits, and is static once done', async () => {
    api.crewTimeline.mockResolvedValue(DONE)
    renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
    expect(await screen.findByTestId('crew-timeline-credits')).toHaveTextContent('1.25 credits')
    expect(screen.getByTestId('crew-timeline-elapsed')).toHaveTextContent('Elapsed')
    expect(screen.getByText('Finished')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-timeline-playhead')).toBeNull()
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
  })

  it('opens a lane on click, but not a closed one', async () => {
    api.crewTimeline.mockResolvedValue(DONE)
    const onOpen = vi.fn()
    renderWith(<CrewTimeline root="root-1" viewing="w-1" active onOpen={onOpen} />)
    const button = await screen.findByRole('button', { name: 'Open Welcome note' })
    expect(button).toHaveAttribute('aria-current', 'true')
    fireEvent.click(button)
    expect(onOpen).toHaveBeenCalledWith('w-1')
    const gone = screen.getAllByTestId('crew-timeline-lane')[2]
    expect(within(gone).queryByRole('button')).toBeNull()
  })

  it('says when it cannot be read', async () => {
    api.crewTimeline.mockRejectedValue(new Error('nope'))
    renderWith(<CrewTimeline root="root-1" viewing={null} active onOpen={vi.fn()} />)
    expect(await screen.findByText('Could not load the timeline.')).toBeInTheDocument()
  })
})

describe('Workers panel List | Timeline switch', () => {
  afterEach(() => {
    vi.clearAllMocks()
    mobile.value = false
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
