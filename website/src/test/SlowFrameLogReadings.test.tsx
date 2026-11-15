import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'

/**
 * What a reader of Settings > Developer > Debug tools sees under the slow frame
 * log's switch. jsdom reports no frames or interactions, so a stand-in observer
 * hands the log its entries.
 *
 * Loaded fresh per test: the log keeps its lists in module scope, and the
 * component must read the same copy of the module the test drives.
 */
type Deliver = (list: { getEntries: () => unknown[] }) => void
let deliverTo: Deliver | null = null
let supported: string[] = []

const frame = (startTime: number, duration: number, blockingDuration: number, scripts: unknown[]) =>
  ({ entryType: 'long-animation-frame', name: 'long-animation-frame', startTime, duration, blockingDuration, scripts })

const press = (interactionId: number, startTime: number, duration: number, target: Node | null) =>
  ({ entryType: 'event', name: 'pointerdown', interactionId, startTime, duration, processingStart: startTime + 4, processingEnd: startTime + duration - 8, target })

async function mount() {
  const log = await import('../dev/slowFrameLog')
  const { SlowFrameLogReadings } = await import('../pages/developer/SlowFrameLogReadings')
  render(<SlowFrameLogReadings />)
  return {
    log,
    deliver: (...entries: unknown[]) => act(() => deliverTo?.({ getEntries: () => entries })),
  }
}

describe('slow frame log readings', () => {
  beforeEach(() => {
    vi.resetModules()
    localStorage.clear()
    deliverTo = null
    supported = ['long-animation-frame', 'event']
    class Fake {
      static get supportedEntryTypes() { return supported }
      constructor(callback: Deliver) { deliverTo = callback }
      observe() {}
      disconnect() {}
    }
    vi.stubGlobal('PerformanceObserver', Fake)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
  })

  it('shows nothing while the log is off', async () => {
    await mount()
    expect(screen.queryByTestId('slow-frame-log')).toBeNull()
  })

  it('lists each slow interaction with its three parts, and each long frame with its scripts', async () => {
    localStorage.setItem('mc-slow-frame-log', '1')
    const { deliver } = await mount()
    expect(screen.getByTestId('slow-frame-log')).toHaveTextContent('Nothing slow recorded yet.')

    const button = document.createElement('button')
    button.setAttribute('aria-label', 'Session options')
    deliver(
      press(1, 1000, 136, button),
      press(2, 3000, 64, null),
      frame(900, 181, 116, [
        { duration: 151, invoker: 'Response.json.then', sourceFunctionName: 'load', sourceURL: 'http://h/assets/first-load-shared-Cm.js', forcedStyleAndLayoutDuration: 26 },
        { duration: 12, invoker: '', sourceFunctionName: '', sourceURL: '' },
      ]),
    )

    expect(screen.getByTestId('slow-frame-log')).not.toHaveTextContent('Nothing slow recorded yet.')

    const rows = screen.getAllByTestId('slow-frame-log-interaction')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('136ms · pointerdown · button [Session options]')
    expect(rows[0]).toHaveTextContent('waited 4ms, handlers 124ms, paint 8ms')
    // A press with no element still reads as a duration and an event type.
    expect(rows[1]).toHaveTextContent('64ms · pointerdown')

    const frames = screen.getAllByTestId('slow-frame-log-frame')
    expect(frames).toHaveLength(1)
    expect(frames[0]).toHaveTextContent('181ms, input blocked for 116ms')
    expect(frames[0]).toHaveTextContent('151ms · Response.json.then · load first-load-shared-Cm.js · forced layout 26ms')
    // A script the browser attributes to nothing shows a placeholder, not a gap.
    expect(frames[0]).toHaveTextContent('12ms · ? · ? · forced layout 0ms')
  })

  it('shows only the first eight rows of a longer list, with the count of all of them', async () => {
    localStorage.setItem('mc-slow-frame-log', '1')
    const { deliver } = await mount()
    deliver(...Array.from({ length: 12 }, (_, i) => press(i + 1, 1000 * i, 60 + i, null)))
    expect(screen.getAllByTestId('slow-frame-log-interaction')).toHaveLength(8)
    // Slowest first: the 71 ms press leads.
    expect(screen.getAllByTestId('slow-frame-log-interaction')[0]).toHaveTextContent('71ms')
    expect(screen.getByTestId('slow-frame-log')).toHaveTextContent('12')
  })

  it('says so when the browser does not report long frames', async () => {
    supported = ['event']
    localStorage.setItem('mc-slow-frame-log', '1')
    await mount()
    expect(screen.getByTestId('slow-frame-log')).toHaveTextContent('This browser does not report long frames.')
  })
})
