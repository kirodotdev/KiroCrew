import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { filterDay, TokenDailyChart } from '../pages/overview/TokenDailyChart'

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
