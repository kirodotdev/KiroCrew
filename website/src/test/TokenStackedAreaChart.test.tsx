import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import type { UsageSeriesPayload } from '../api/client'
import {
  TokenStackedAreaChart,
  channelLabel,
  nearestIndex,
  niceTop,
  stackPaths,
  startedByDay,
  toCumulative,
  weekMondayOf,
} from '../pages/overview/TokenStackedAreaChart'

const { usageSeries } = vi.hoisted(() => ({ usageSeries: vi.fn() }))

vi.mock('../api/client', () => ({ api: { usageSeries } }))
// The chart colours its layers from the session palette, which reads the theme
// context; a fixed palette keeps the test on the chart's own behaviour.
vi.mock('../hooks/useSessionPalette', () => ({
  useSessionPalette: () => ({ paletteColors: ['#ff0000', '#00ff00', '#0000ff'] }),
}))
// The Layer by control is a SimpleSelect, which renders a native <select> on a
// touch device and a Radix listbox otherwise; the native path is the one a
// change event can drive, and the chart's behaviour is the same on both.
vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => true }))

function payload(overrides: Partial<UsageSeriesPayload> = {}): UsageSeriesPayload {
  return {
    dates: ['2026-09-30', '2026-10-01', '2026-10-02'],
    series: [
      { key: 'dashboard', kind: 'bucket', values: [5, 0, 10], total: 15 },
      { key: 'cron', kind: 'bucket', values: [1, 1, 1], total: 3 },
      { key: '__other__', kind: 'other', values: [0, 2, 0], total: 2, members: 4 },
      { key: '__unattributed__', kind: 'unattributed', values: [0, 0, 0.5], total: 0.5 },
    ],
    total: 20.5,
    truncated: false,
    dropped_rows: 0,
    complete_from: null,
    ...overrides,
  }
}

describe('toCumulative', () => {
  it('prefix-sums every layer and keeps the window total', () => {
    const [a, b] = toCumulative(payload().series)

    expect(a.values).toEqual([5, 5, 15])
    expect(b.values).toEqual([1, 2, 3])
    expect(a.total).toBe(15)
  })
})

describe('nearestIndex', () => {
  it('maps a pointer fraction onto the nearest day and clamps the edges', () => {
    expect(nearestIndex(0, 30)).toBe(0)
    expect(nearestIndex(1, 30)).toBe(29)
    expect(nearestIndex(0.5, 31)).toBe(15)
    expect(nearestIndex(-2, 30)).toBe(0)
    expect(nearestIndex(7, 30)).toBe(29)
    expect(nearestIndex(0.9, 1)).toBe(0)
  })
})

describe('niceTop', () => {
  it('rounds the axis top up to a round number so the ticks read cleanly', () => {
    // 1602.2 would have printed "1.6K" over a mid tick of "801.1".
    expect(niceTop(1602.2)).toBe(2000)
    expect(niceTop(801.1)).toBe(1000)
    expect(niceTop(1000)).toBe(1000)
    expect(niceTop(0)).toBeGreaterThan(0)
  })
})

describe('channelLabel', () => {
  it('shows channel ids as words and keeps the unrecognised bucket apart from the fold', () => {
    expect(channelLabel('dashboard')).toBe('Dashboard')
    expect(channelLabel('workflow_pool')).toBe('Workflow pool')
    expect(channelLabel('other')).toBe('Unrecognized sessions')
  })
})

describe('stackPaths', () => {
  it('stacks bottom-first and scales to a rounded axis top above the day total', () => {
    const { paths, top } = stackPaths(payload().series, 3)

    expect(paths).toHaveLength(4)
    expect(paths.every(p => p.startsWith('M'))).toBe(true)
    // Day 3: 10 + 1 + 0 + 0.5 = 11.5 is the tallest column; the axis rounds up.
    expect(top).toBe(12)
  })

  it('never divides by zero on an all-zero window', () => {
    const empty = payload().series.map(s => ({ ...s, values: [0, 0, 0] }))

    expect(() => stackPaths(empty, 3)).not.toThrow()
  })
})

let client: QueryClient

function mount() {
  return render(
    <QueryClientProvider client={client}>
      <Provider store={createTestStore()}>
        <TokenStackedAreaChart />
      </Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  usageSeries.mockReset().mockResolvedValue(payload())
})
afterEach(() => {
  cleanup()
  client.clear()
})

describe('weekMondayOf / startedByDay', () => {
  it.each([
    ['2026-10-04', '2026-09-28'],   // Sunday: the day a `getDate() - getDay() + 1` would push forward
    ['2026-10-05', '2026-10-05'],   // Monday
    ['2026-10-06', '2026-10-05'],   // Tuesday
    ['2026-10-07', '2026-10-05'],   // Wednesday
    ['2026-10-08', '2026-10-05'],   // Thursday
    ['2026-10-09', '2026-10-05'],   // Friday
    ['2026-10-10', '2026-10-05'],   // Saturday
    ['2027-01-01', '2026-12-28'],   // year rollover
    ['2026-03-01', '2026-02-23'],   // month rollover
  ])('keys %s to the Monday of its ISO week, %s, as the server does', (day, monday) => {
    expect(weekMondayOf(day)).toBe(monday)
  })

  it('names the Sunday closing the week, clamped to the last day on the axis for a week still running', () => {
    expect(startedByDay('2026-09-28', '2026-10-10')).toBe('2026-10-04')
    expect(startedByDay('2026-12-28', '2027-01-10')).toBe('2027-01-03')
    expect(startedByDay('2026-10-05', '2026-10-08')).toBe('2026-10-08')
  })
})

describe('TokenStackedAreaChart', () => {
  it('requests the default view and draws one path per layer with a legend', async () => {
    mount()

    await screen.findByTestId('usage-series-chart')

    expect(usageSeries).toHaveBeenCalledWith('channel')
    expect(document.querySelectorAll('path[data-layer]')).toHaveLength(4)
    const legend = screen.getByTestId('usage-series-legend')
    expect(legend).toHaveTextContent('Dashboard')
    expect(legend).toHaveTextContent('Cron')
    expect(legend).toHaveTextContent('Other (4 more)')
    expect(legend).toHaveTextContent('Unattributed')
    // The Unattributed layer earns a visible caption, not a hover-only title.
    expect(screen.getByTestId('usage-series-unattributed-caption')).toHaveTextContent(
      "Unattributed: turns that didn't record this detail.",
    )
    expect(screen.queryByTestId('usage-series-truncated')).toBeNull()
  })

  it('shows no unattributed caption when every turn is attributed', async () => {
    usageSeries.mockResolvedValue(payload({
      series: [{ key: 'dashboard', kind: 'bucket', values: [5, 0, 10], total: 15 }],
      total: 15,
    }))

    mount()

    await screen.findByTestId('usage-series-chart')
    expect(screen.queryByTestId('usage-series-unattributed-caption')).toBeNull()
  })

  it('says how many of the oldest turns the server left out and from which day the window is whole', async () => {
    usageSeries.mockResolvedValue(payload({ truncated: true, dropped_rows: 1234, complete_from: '2026-10-01' }))

    mount()

    await screen.findByTestId('usage-series-chart')
    const notice = screen.getByTestId('usage-series-truncated')
    expect(notice).toHaveTextContent("Totals undercount: the oldest 1,234 turns weren't counted; each day from Oct 1, 2026 counts all of its turns.")
    expect(notice).toHaveClass('text-warn')
  })

  it('falls back to the count alone when the cap tripped inside the newest day', async () => {
    usageSeries.mockResolvedValue(payload({ truncated: true, dropped_rows: 1234, complete_from: null }))

    mount()

    await screen.findByTestId('usage-series-chart')
    expect(screen.getByTestId('usage-series-truncated')).toHaveTextContent(
      "Totals undercount: the oldest 1,234 turns weren't counted.",
    )
  })

  it('keeps the previous dimension on screen, dimmed, until the next one answers', async () => {
    mount()
    await screen.findByTestId('usage-series-chart')
    let answer!: (p: UsageSeriesPayload) => void
    usageSeries.mockImplementationOnce(() => new Promise<UsageSeriesPayload>(resolve => { answer = resolve }))

    fireEvent.change(screen.getByRole('combobox', { name: 'Layer by' }), { target: { value: 'agent' } })

    // The channel stack stands in for the agent one: no skeleton, busy and
    // dimmed, and still labelled as channels rather than as raw agent keys.
    expect(document.querySelector('.skeleton')).toBeNull()
    expect(screen.getByTestId('usage-series-chart')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('usage-series-body')).toHaveAttribute('aria-busy', 'true'))
    expect(usageSeries).toHaveBeenLastCalledWith('agent')
    expect(screen.getByTestId('usage-series-body')).toHaveClass('opacity-60')
    expect(screen.getByTestId('usage-series-legend')).toHaveTextContent('Dashboard')
    // The stale legend is labelled in words, not only by the dimming.
    expect(screen.getByTestId('usage-series-loading')).toHaveTextContent('Loading…')
    expect(screen.getByRole('status')).not.toHaveClass('sr-only')

    answer(payload({ series: [{ key: 'reviewer', kind: 'bucket', values: [1, 1, 1], total: 3 }], total: 3 }))

    await waitFor(() => expect(screen.getByTestId('usage-series-legend')).toHaveTextContent('reviewer'))
    expect(screen.getByTestId('usage-series-body')).not.toHaveAttribute('aria-busy')
    expect(screen.getByTestId('usage-series-body')).not.toHaveClass('opacity-60')
    // The live region stays mounted (so the next change is announced) but empties.
    expect(screen.getByTestId('usage-series-loading')).toBeEmptyDOMElement()
    expect(JSON.parse(localStorage.getItem('kc.usageSeries.v1') ?? '{}')).toMatchObject({ by: 'agent' })
  })

  it('names session-start-week layers by when their sessions were first seen, and the first week by when its sessions had started by', async () => {
    // The window opens on Wed Sep 30, inside the ISO week of Mon Sep 28. A
    // session first seen that week may have started any time up to Sun Oct 4
    // -- or long before the window -- so that layer is not a "week of" starts.
    const dates = ['2026-09-30', '2026-10-01', '2026-10-02', '2026-10-03', '2026-10-04', '2026-10-05', '2026-10-06']
    usageSeries.mockResolvedValue(payload({
      by: 'cohort',
      dates,
      series: [
        { key: '2026-09-28', kind: 'bucket', values: [1, 1, 1, 1, 1, 1, 1], total: 7 },
        { key: '2026-10-05', kind: 'bucket', values: [0, 0, 0, 0, 0, 2, 2], total: 4 },
      ],
      total: 11,
    }))
    localStorage.setItem('kc.usageSeries.v1', JSON.stringify({ by: 'cohort', cumulative: true }))

    mount()

    await screen.findByTestId('usage-series-legend')
    expect(usageSeries).toHaveBeenCalledWith('cohort')
    const legend = screen.getByTestId('usage-series-legend')
    expect(legend).toHaveTextContent('Started by Oct 4')
    expect(legend).toHaveTextContent('Week of Oct 5')
    expect(legend).not.toHaveTextContent('Week of Sep 28')
    // The slider's value text reads the same label out.
    expect(screen.getByRole('slider')).toHaveAttribute('aria-valuetext', expect.stringContaining('Started by Oct 4'))
    expect(screen.getByTestId('usage-series-cohort-caption')).toHaveTextContent(
      'Layers group spend by the week each session was first seen in this window; a session that began earlier counts from its first turn here, so the oldest layer, “Started by” a date, also holds older sessions.',
    )
  })

  it('puts "Started by" on the oldest cohort drawn when the window opens on a quiet weekend, clamped to the axis for a week still running', async () => {
    // Sun Oct 4 opens the window with no turns; the first session is seen on
    // Mon Oct 5, so the server sends no layer for the week of Sep 28. The
    // sessions already running when the window opened are in the Oct 5 layer,
    // which must be the one named "Started by"; its week is still running on
    // the axis's last day, Thu Oct 8, so that is the date it names.
    usageSeries.mockResolvedValue(payload({
      by: 'cohort',
      dates: ['2026-10-04', '2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08'],
      series: [{ key: '2026-10-05', kind: 'bucket', values: [0, 3, 3, 3, 3], total: 12 }],
      total: 12,
    }))
    localStorage.setItem('kc.usageSeries.v1', JSON.stringify({ by: 'cohort', cumulative: true }))

    mount()

    await screen.findByTestId('usage-series-legend')
    const legend = screen.getByTestId('usage-series-legend')
    expect(legend).toHaveTextContent('Started by Oct 8')
    expect(legend).not.toHaveTextContent('Week of')
  })

  it('shows no cohort caption for the other dimensions', async () => {
    mount()

    await screen.findByTestId('usage-series-chart')
    expect(screen.queryByTestId('usage-series-cohort-caption')).toBeNull()
  })

  it('persists the cumulative switch and re-stacks without refetching', async () => {
    mount()
    await screen.findByTestId('usage-series-chart')
    const before = document.querySelector('path[data-layer="dashboard"]')?.getAttribute('d')

    fireEvent.click(screen.getByRole('switch', { name: 'Cumulative' }))

    await waitFor(() => {
      expect(document.querySelector('path[data-layer="dashboard"]')?.getAttribute('d')).not.toBe(before)
    })
    expect(usageSeries).toHaveBeenCalledTimes(1)
    expect(JSON.parse(localStorage.getItem('kc.usageSeries.v1') ?? '{}')).toMatchObject({ cumulative: false })
  })

  it('shows the hovered day and every one of its layers in a tooltip', async () => {
    // Nine spending layers: the top seven plus Other plus Unattributed, the most
    // the backend ever returns. The tooltip must list them all, or its rows
    // would not add up to the total it prints.
    const series = Array.from({ length: 7 }, (_, i) => ({
      key: `agent-${i}`, kind: 'bucket' as const, values: [1, 1, 1], total: 3,
    }))
    series.push({ key: '__other__', kind: 'other' as never, values: [1, 1, 1], total: 3 })
    series.push({ key: '__unattributed__', kind: 'unattributed' as never, values: [1, 1, 1], total: 3 })
    usageSeries.mockResolvedValue(payload({ series, total: 27 }))
    localStorage.setItem('kc.usageSeries.v1', JSON.stringify({ by: 'agent', cumulative: true }))
    mount()
    await screen.findByTestId('usage-series-chart')
    const plot = screen.getByTestId('usage-series-plot')
    plot.getBoundingClientRect = () => ({ left: 0, width: 300, top: 0, height: 100, right: 300, bottom: 100, x: 0, y: 0, toJSON: () => ({}) })

    fireEvent.pointerMove(plot, { clientX: 299 })

    const tip = await screen.findByTestId('usage-series-tooltip')
    expect(tip).toHaveTextContent('Oct 2, 2026')
    expect(tip).toHaveTextContent('Total to date: 27')
    expect(tip.querySelectorAll('.tabular-nums')).toHaveLength(9)
    expect(tip).toHaveTextContent('Unattributed')
  })

  it('steps through days from the keyboard and reads the day out as the slider value', async () => {
    mount()
    await screen.findByTestId('usage-series-chart')
    const plot = screen.getByRole('slider')

    fireEvent.focus(plot)
    expect(plot).toHaveAttribute('aria-valuenow', '2')
    expect(plot).toHaveAttribute('aria-valuetext', expect.stringContaining('Oct 2, 2026'))
    expect(plot).toHaveAttribute('aria-valuetext', expect.stringContaining('Total to date: 20.5'))

    fireEvent.keyDown(plot, { key: 'ArrowLeft' })
    fireEvent.keyDown(plot, { key: 'ArrowLeft' })
    expect(plot).toHaveAttribute('aria-valuenow', '0')
    expect(plot).toHaveAttribute('aria-valuetext', expect.stringContaining('Sep 30, 2026'))
    expect(screen.getByTestId('usage-series-tooltip')).toHaveTextContent('Dashboard5.00')

    fireEvent.keyDown(plot, { key: 'ArrowLeft' })
    expect(plot).toHaveAttribute('aria-valuenow', '0')
    fireEvent.keyDown(plot, { key: 'End' })
    expect(plot).toHaveAttribute('aria-valuenow', '2')
    fireEvent.keyDown(plot, { key: 'Escape' })
    expect(screen.queryByTestId('usage-series-tooltip')).toBeNull()
  })

  it('says so when nothing was spent', async () => {
    usageSeries.mockResolvedValue(payload({ series: [], total: 0 }))

    mount()

    expect(await screen.findByText('No spend recorded yet.')).toBeInTheDocument()
    expect(screen.queryByTestId('usage-series-chart')).toBeNull()
  })

  it('a failed refresh over the empty state reads like a failed first load, not "last values" beside "no spend"', async () => {
    usageSeries.mockResolvedValue(payload({ series: [], total: 0 }))
    mount()
    await screen.findByText('No spend recorded yet.')

    usageSeries.mockRejectedValue(new Error('series unavailable'))
    await client.refetchQueries({ queryKey: ['usage-series'] })

    expect(await screen.findByText('series unavailable')).toBeInTheDocument()
    expect(screen.getByText("Couldn't load spend over time")).toBeInTheDocument()
    expect(screen.queryByText('No spend recorded yet.')).toBeNull()
    expect(screen.queryByText(/showing the last values/)).toBeNull()
  })

  it('a failed refresh over a drawn chart says so above the stack it keeps showing, the server text on its own line', async () => {
    mount()
    await screen.findByTestId('usage-series-chart')
    usageSeries.mockRejectedValue(new Error('series unavailable'))

    await client.refetchQueries({ queryKey: ['usage-series'] })

    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent('Can’t refresh — showing the last values we read.')
    expect(screen.getByText('series unavailable')).toHaveClass('block')
    expect(screen.getByTestId('usage-series-chart')).toBeInTheDocument()
    expect(screen.getByTestId('usage-series-legend')).toHaveTextContent('Dashboard')
  })

  it('a failed first load names what could not be loaded and offers to try again', async () => {
    usageSeries.mockRejectedValue(new Error('series unavailable'))
    mount()
    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent("Couldn't load spend over time")
    expect(notice).toHaveTextContent('series unavailable')
    // The server's text sits on its own line under the title, not glued to it.
    expect(screen.getByText('series unavailable')).toHaveClass('block')
    usageSeries.mockResolvedValue(payload())

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))

    await screen.findByTestId('usage-series-chart')
    expect(usageSeries).toHaveBeenCalledTimes(2)
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
