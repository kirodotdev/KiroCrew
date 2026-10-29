/**
 * G6: a live playhead tick re-renders the playhead and the elapsed label only.
 * The scale (breakpoints over every bar, then a sort) is derived once per read.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const api = vi.hoisted(() => ({ crewTimeline: vi.fn() }))
vi.mock('../../../api/client', () => ({ api }))
vi.mock('../../../hooks/useReducedMotion', () => ({ useReducedMotion: () => false }))
const spy = vi.hoisted(() => ({ scale: 0 }))
vi.mock('../../../lib/crewTimeline', async importOriginal => {
  const real = await importOriginal<typeof import('../../../lib/crewTimeline')>()
  return { ...real, timelineScale: (...args: Parameters<typeof real.timelineScale>) => { spy.scale += 1; return real.timelineScale(...args) } }
})

import CrewTimeline from './CrewTimeline'

const at = (s: number) => new Date(Date.UTC(2026, 9, 21, 3, 0, s)).toISOString()
const LIVE = {
  root: 'r', now: at(90), live: true, started: at(0), ended: at(90), elapsed_ms: 90_000, credits: 0,
  lanes: [{
    key: 'r', title: 'Root', agent: '', model: 'm', model_source: 'served', role: 'conductor', status: 'working',
    running: true, waiting: false, credits: 0, first_ts: at(0), last_ts: at(60), dropped_segments: 0, marks: [],
    segments: [{ kind: 'turn', start: at(0), end: at(60), end_bound: 'recorded', label: '' }],
  }],
  handoffs: [], gaps: [],
}

describe('CrewTimeline ticking', () => {
  afterEach(() => { vi.useRealTimers(); spy.scale = 0 })

  it('advances the playhead without re-deriving the scale', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    api.crewTimeline.mockResolvedValue(LIVE)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><CrewTimeline root="r" viewing={null} active onOpen={vi.fn()} /></QueryClientProvider>)
    const before = (await screen.findByTestId('crew-timeline-playhead')).style.left
    const derived = spy.scale
    await act(async () => { vi.advanceTimersByTime(1000) })
    expect(screen.getByTestId('crew-timeline-playhead').style.left).not.toBe(before)
    expect(spy.scale).toBe(derived)
  })

  it('freezes the clock where it is when the person starts typing, never rewinds it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    api.crewTimeline.mockResolvedValue(LIVE)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><CrewTimeline root="r" viewing={null} active onOpen={vi.fn()} /></QueryClientProvider>)
    const read = (await screen.findByTestId('crew-timeline-elapsed')).textContent
    await act(async () => { vi.advanceTimersByTime(5000) })
    const advanced = screen.getByTestId('crew-timeline-elapsed').textContent
    expect(advanced).not.toBe(read)
    // A keystroke in the composer pauses the clock: it stays at the advanced time.
    const area = document.createElement('div')
    area.className = 'input-area'
    const box = document.createElement('textarea')
    area.appendChild(box)
    document.body.appendChild(area)
    await act(async () => { box.dispatchEvent(new Event('input', { bubbles: true })) })
    expect(screen.getByTestId('crew-timeline-elapsed').textContent).toBe(advanced)
    area.remove()
  })
})
