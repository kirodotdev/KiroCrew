import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { calendarDays, filterDay, TokenDailyChart } from '../pages/overview/TokenDailyChart'

const ALL = '__all__'

const bucket = (input: number, output: number) => ({
  input, output, cacheCreate: 0, cacheRead: 0, costUsd: 0,
})

const sampleDay = {
  date: '2026-05-14',
  input: 1000,
  output: 200,
  cacheCreate: 0,
  cacheRead: 0,
  costUsd: 0.05,
  models: {
    'claude-sonnet-4': bucket(800, 150),
    opus: bucket(200, 50),
  },
  providers: {
    opencode: bucket(800, 150),
    claude_code: bucket(200, 50),
  },
  providerModels: {
    opencode: { 'claude-sonnet-4': bucket(800, 150) },
    claude_code: { opus: bucket(200, 50) },
  },
}

describe('filterDay', () => {
  it('returns daily totals when both filters are ALL', () => {
    const r = filterDay(sampleDay, ALL, ALL)
    expect(r).toEqual({ input: 1000, output: 200, cacheCreate: 0, cacheRead: 0, costUsd: 0.05 })
  })

  it('returns provider bucket when only provider is set', () => {
    const r = filterDay(sampleDay, 'opencode', ALL)
    expect(r.input).toBe(800)
    expect(r.output).toBe(150)
  })

  it('returns model bucket when only model is set', () => {
    const r = filterDay(sampleDay, ALL, 'opus')
    expect(r.input).toBe(200)
    expect(r.output).toBe(50)
  })

  it('returns intersection bucket from providerModels when both are set', () => {
    const r = filterDay(sampleDay, 'claude_code', 'opus')
    expect(r.input).toBe(200)
    expect(r.output).toBe(50)
  })

  it('returns empty bucket for invalid provider+model pair', () => {
    // opencode never produced opus tokens → invalid provider+model pair.
    const r = filterDay(sampleDay, 'opencode', 'opus')
    expect(r).toEqual({ input: 0, output: 0, cacheCreate: 0, cacheRead: 0, costUsd: 0 })
  })

  it('returns empty bucket when providerModels is missing', () => {
    const day = { ...sampleDay, providerModels: undefined }
    const r = filterDay(day, 'opencode', 'claude-sonnet-4')
    expect(r).toEqual({ input: 0, output: 0, cacheCreate: 0, cacheRead: 0, costUsd: 0 })
  })

  it('returns empty bucket when provider has no record on that day', () => {
    const r = filterDay(sampleDay, 'unknown-provider', ALL)
    expect(r).toEqual({ input: 0, output: 0, cacheCreate: 0, cacheRead: 0, costUsd: 0 })
  })
})

describe('TokenDailyChart cascading filters', () => {
  const history = [sampleDay]
  const providers = ['opencode', 'claude_code']
  const models = ['claude-sonnet-4', 'opus']
  const providerModels = {
    opencode: ['claude-sonnet-4'],
    claude_code: ['opus'],
  }

  // The filters are SimpleSelect (Radix) triggers, not native <select>: located
  // by accessible name (aria-label), never by index. A `change` event on the
  // trigger does nothing — open it, then click the option.
  const providerTrigger = () => screen.getByRole('combobox', { name: 'Provider' })
  const modelTrigger = () => screen.getByRole('combobox', { name: 'Model' })

  /** Open a trigger and read the option labels, then close without selecting. */
  async function readOptions(trigger: HTMLElement) {
    fireEvent.click(trigger)
    await screen.findByRole('option', { name: 'All' })
    const labels = screen.getAllByRole('option').map(o => o.textContent)
    fireEvent.keyDown(document.activeElement || document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('option', { name: 'All' })).toBeNull())
    return labels
  }

  /** Open a trigger and pick the option with this exact label. */
  async function pick(trigger: HTMLElement, name: string) {
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByRole('option', { name }))
  }

  it('renders both provider and model dropdowns', () => {
    render(
      <TokenDailyChart
        history={history}
        providers={providers}
        models={models}
        providerModels={providerModels}
      />
    )

    expect(screen.getByText('Provider')).toBeInTheDocument()
    expect(screen.getByText('Model')).toBeInTheDocument()
    // ALL is the sentinel value; the trigger renders its label.
    expect(providerTrigger()).toHaveTextContent('All')
    expect(modelTrigger()).toHaveTextContent('All')
  })

  it('lists all global model options when provider is ALL', async () => {
    render(
      <TokenDailyChart
        history={history}
        providers={providers}
        models={models}
        providerModels={providerModels}
      />
    )

    // ALL + global models
    expect(await readOptions(modelTrigger())).toEqual(['All', 'claude-sonnet-4', 'opus'])
  })

  it('cascades model dropdown to only models valid for the selected provider', async () => {
    render(
      <TokenDailyChart
        history={history}
        providers={providers}
        models={models}
        providerModels={providerModels}
      />
    )

    await pick(providerTrigger(), 'opencode')

    const modelOptionLabels = await readOptions(modelTrigger())
    // opencode only ever paired with claude-sonnet-4 → opus must NOT appear.
    expect(modelOptionLabels).toEqual(['All', 'claude-sonnet-4'])
    expect(modelOptionLabels).not.toContain('opus')
  })

  it('resets model selection when it becomes invalid for the new provider', async () => {
    render(
      <TokenDailyChart
        history={history}
        providers={providers}
        models={models}
        providerModels={providerModels}
      />
    )

    // Start with claude_code + opus (valid pair).
    await pick(providerTrigger(), 'claude_code')
    await pick(modelTrigger(), 'opus')
    await waitFor(() => expect(modelTrigger()).toHaveTextContent('opus'))

    // Switch provider to opencode — opus is no longer valid.
    await pick(providerTrigger(), 'opencode')

    await waitFor(() => expect(modelTrigger()).toHaveTextContent('All'))
  })

  it('falls back to global model list when providerModels is missing', async () => {
    render(
      <TokenDailyChart
        history={history}
        providers={providers}
        models={models}
      />
    )

    await pick(providerTrigger(), 'opencode')

    // No cascade data → keep the global model list (back-compat).
    expect(await readOptions(modelTrigger())).toEqual(['All', 'claude-sonnet-4', 'opus'])
  })
})

// The bars are drawn and each day's figures sit in a tooltip that shows only
// under a pointer, so a screen reader reached no figure at all. The same
// figures are now a list, one item per day, with the tooltip hidden from
// assistive technology so nothing is read twice.
describe('TokenDailyChart figures for assistive technology', () => {
  const days = [
    { ...sampleDay, date: '2026-05-13', input: 1500, output: 300 },
    sampleDay,
  ]

  it('lists every day with its tokens and cost', () => {
    render(<TokenDailyChart history={days} />)
    const items = screen.getAllByRole('listitem')
    // One item per calendar day, so an idle day is announced too.
    expect(items).toHaveLength(14)
    expect(items[12]).toHaveTextContent('2026-05-13')
    expect(items[13]).toHaveTextContent('2026-05-14')
    expect(items[13]).toHaveTextContent('$0.05')
  })

  it('keeps the pointer tooltip out of the accessibility tree', () => {
    const { container } = render(<TokenDailyChart history={days} />)
    const tooltip = container.querySelector('.pointer-events-none')
    expect(tooltip?.getAttribute('aria-hidden')).toBe('true')
  })
})

// The history lists only the days that recorded a turn. Drawn as its last 14
// entries, a quiet week vanished and the bars read as consecutive days, so two
// busy days ten days apart sat side by side with nothing between them.
describe('TokenDailyChart calendar days', () => {
  const tooltips = (container: HTMLElement) => Array.from(container.querySelectorAll('.pointer-events-none'))

  it('draws the 14 calendar days ending at the gateway day, idle ones empty', () => {
    const days = [
      { ...sampleDay, date: '2026-05-03' },
      { ...sampleDay, date: '2026-05-13' },
    ]
    const { container } = render(<TokenDailyChart history={days} endDate="2026-05-16" />)
    const bars = tooltips(container)
    expect(bars).toHaveLength(14)
    expect(bars[0]).toHaveTextContent('2026-05-03')
    expect(bars[10]).toHaveTextContent('2026-05-13')
    expect(bars[13]).toHaveTextContent('2026-05-16')
  })

  it('ends at the last recorded day when no gateway day is given', () => {
    const { container } = render(<TokenDailyChart history={[sampleDay]} />)
    const bars = tooltips(container)
    expect(bars).toHaveLength(14)
    expect(bars[13]).toHaveTextContent('2026-05-14')
    expect(bars[0]).toHaveTextContent('2026-05-01')
  })

  it('ignores a gateway day that is not a real calendar day', () => {
    const { container } = render(<TokenDailyChart history={[sampleDay]} endDate="2026-02-30" />)
    const bars = tooltips(container)
    expect(bars).toHaveLength(14)
    expect(bars[13]).toHaveTextContent('2026-05-14')
  })

  it('steps across a month boundary without skipping or repeating a day', () => {
    expect(calendarDays('2026-03-02', 4)).toEqual(['2026-02-27', '2026-02-28', '2026-03-01', '2026-03-02'])
  })
})
