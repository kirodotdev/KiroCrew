import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/react'

const report = vi.fn()
let behave: () => unknown = () => Promise.resolve(undefined)
vi.mock('../api/client', () => ({
  api: {
    reportAppView: (body: unknown) => {
      report(body)
      return behave()
    },
  },
}))

import { reportAppView, useAppViewMetrics } from '../lib/appViewTelemetry'
import { LocalOverlayRoot } from '../components/overlayRoot'

function View({ app, label }: { app: string | null; label?: string }) {
  useAppViewMetrics(app)
  return <div>{label}</div>
}

let visibility: DocumentVisibilityState = 'visible'
function setVisibility(next: DocumentVisibilityState) {
  visibility = next
  document.dispatchEvent(new Event('visibilitychange'))
}

const opened = (app = 'weather-app', surface = 'page') => ({ app, surface, event: 'opened' })
const span = (seconds: number, app = 'weather-app') => ({ app, surface: 'page', event: 'foreground', seconds })

describe('useAppViewMetrics', () => {
  beforeEach(() => {
    report.mockReset()
    behave = () => Promise.resolve(undefined)
    visibility = 'visible'
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility })
    vi.useFakeTimers({ toFake: ['Date', 'performance', 'setTimeout', 'clearTimeout'] })
    vi.setSystemTime(new Date('2026-11-20T00:00:00Z'))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('sends one opened per mount and nothing on a re-render', () => {
    const { rerender } = render(<View app="weather-app" label="a" />)
    rerender(<View app="weather-app" label="b" />)
    rerender(<View app="weather-app" label="c" />)
    expect(report.mock.calls).toEqual([[opened()]])
  })

  it('sends one span when the page is hidden, and none again on unmount', () => {
    const { unmount } = render(<View app="weather-app" />)
    vi.advanceTimersByTime(5_400)
    setVisibility('hidden')
    vi.advanceTimersByTime(60_000)
    unmount()
    expect(report.mock.calls).toEqual([[opened()], [span(5)]])
  })

  it('starts a new span when the page is shown again, and ends it on pagehide', () => {
    render(<View app="weather-app" />)
    vi.advanceTimersByTime(2_000)
    setVisibility('hidden')
    vi.advanceTimersByTime(10_000)
    setVisibility('visible')
    vi.advanceTimersByTime(3_000)
    window.dispatchEvent(new Event('pagehide'))
    expect(report.mock.calls).toEqual([[opened()], [span(2)], [span(3)]])
  })

  it('sends nothing for a span under one second', () => {
    const { unmount } = render(<View app="weather-app" />)
    vi.advanceTimersByTime(999)
    setVisibility('hidden')
    unmount()
    expect(report.mock.calls).toEqual([[opened()]])
  })

  it('does not count time a page mounted hidden spent hidden', () => {
    visibility = 'hidden'
    const { unmount } = render(<View app="weather-app" />)
    vi.advanceTimersByTime(30_000)
    unmount()
    expect(report.mock.calls).toEqual([[opened()]])
  })

  it('ends the old app span and opens the new app when the app changes', () => {
    const { rerender } = render(<View app="weather-app" />)
    vi.advanceTimersByTime(4_000)
    rerender(<View app="notes" />)
    expect(report.mock.calls).toEqual([[opened()], [span(4)], [opened('notes')]])
  })

  it('pauses the span while another Crew hides this pane, with no second opened', () => {
    const { rerender, unmount } = render(<LocalOverlayRoot hidden={false}><View app="weather-app" /></LocalOverlayRoot>)
    vi.advanceTimersByTime(3_000)
    rerender(<LocalOverlayRoot hidden><View app="weather-app" /></LocalOverlayRoot>)
    vi.advanceTimersByTime(600_000)
    rerender(<LocalOverlayRoot hidden={false}><View app="weather-app" /></LocalOverlayRoot>)
    vi.advanceTimersByTime(7_000)
    unmount()
    expect(report.mock.calls).toEqual([[opened()], [span(3)], [span(7)]])
  })

  it('does not count time a page mounted behind another Crew', () => {
    const { unmount } = render(<LocalOverlayRoot hidden><View app="weather-app" /></LocalOverlayRoot>)
    vi.advanceTimersByTime(30_000)
    unmount()
    expect(report.mock.calls).toEqual([[opened()]])
  })

  it('measures a span on the monotonic clock, not the wall clock', () => {
    const { unmount } = render(<View app="weather-app" />)
    vi.advanceTimersByTime(5_000)
    // The system clock jumps an hour forward; the time on screen is still 5 s.
    vi.setSystemTime(Date.now() + 3_600_000)
    setVisibility('hidden')
    unmount()
    expect(report.mock.calls).toEqual([[opened()], [span(5)]])
  })

  it('pauses the span while its frame is display:none (zero-size viewport), with no second opened', () => {
    const { unmount } = render(<View app="weather-app" />)
    const size = (w: number, h: number) => {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: w })
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: h })
      window.dispatchEvent(new Event('resize'))
    }
    try {
      vi.advanceTimersByTime(10_000)
      size(0, 0)
      vi.advanceTimersByTime(600_000)
      size(1024, 768)
      vi.advanceTimersByTime(4_000)
      unmount()
    } finally {
      size(1024, 768)
    }
    expect(report.mock.calls).toEqual([[opened()], [span(10)], [span(4)]])
  })

  it('counts nothing without an app', () => {
    const { unmount } = render(<View app={null} />)
    vi.advanceTimersByTime(5_000)
    unmount()
    expect(report).not.toHaveBeenCalled()
  })
})

describe('reportAppView', () => {
  beforeEach(() => {
    report.mockReset()
  })

  it('swallows a rejected request', async () => {
    behave = () => Promise.reject(new Error('offline'))
    expect(() => reportAppView({ app: 'x', surface: 'desktop', event: 'opened' })).not.toThrow()
    await Promise.resolve()
  })

  it('swallows a synchronous throw', () => {
    behave = () => {
      throw new Error('boom')
    }
    expect(() => reportAppView({ app: 'x', surface: 'desktop', event: 'opened' })).not.toThrow()
  })
})
