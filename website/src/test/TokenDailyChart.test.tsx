import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
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

  function getSelects() {
    // Provider + Model are the only two selects in the chart.
    return screen.getAllByRole('combobox') as HTMLSelectElement[]
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
    const [providerSel, modelSel] = getSelects()
    expect(providerSel.value).toBe(ALL)
    expect(modelSel.value).toBe(ALL)
  })

  it('lists all global model options when provider is ALL', () => {
    render(
      <TokenDailyChart
        history={history}
        providers={providers}
        models={models}
        providerModels={providerModels}
      />
    )

    const [, modelSel] = getSelects()
    const modelOptionValues = within(modelSel)
      .getAllByRole('option')
      .map(o => (o as HTMLOptionElement).value)
    // ALL + global models
    expect(modelOptionValues).toEqual([ALL, 'claude-sonnet-4', 'opus'])
  })

  it('cascades model dropdown to only models valid for the selected provider', () => {
    render(
      <TokenDailyChart
        history={history}
        providers={providers}
        models={models}
        providerModels={providerModels}
      />
    )

    const [providerSel, modelSel] = getSelects()
    fireEvent.change(providerSel, { target: { value: 'opencode' } })

    const modelOptionValues = within(modelSel)
      .getAllByRole('option')
      .map(o => (o as HTMLOptionElement).value)
    // opencode only ever paired with claude-sonnet-4 → opus must NOT appear.
    expect(modelOptionValues).toEqual([ALL, 'claude-sonnet-4'])
    expect(modelOptionValues).not.toContain('opus')
  })

  it('resets model selection when it becomes invalid for the new provider', () => {
    render(
      <TokenDailyChart
        history={history}
        providers={providers}
        models={models}
        providerModels={providerModels}
      />
    )

    const [providerSel, modelSel] = getSelects()
    // Start with claude_code + opus (valid pair).
    fireEvent.change(providerSel, { target: { value: 'claude_code' } })
    fireEvent.change(modelSel, { target: { value: 'opus' } })
    expect(modelSel.value).toBe('opus')

    // Switch provider to opencode — opus is no longer valid.
    fireEvent.change(providerSel, { target: { value: 'opencode' } })

    expect(modelSel.value).toBe(ALL)
  })

  it('falls back to global model list when providerModels is missing', () => {
    render(
      <TokenDailyChart
        history={history}
        providers={providers}
        models={models}
      />
    )

    const [providerSel, modelSel] = getSelects()
    fireEvent.change(providerSel, { target: { value: 'opencode' } })

    const modelOptionValues = within(modelSel)
      .getAllByRole('option')
      .map(o => (o as HTMLOptionElement).value)
    // No cascade data → keep the global model list (back-compat).
    expect(modelOptionValues).toEqual([ALL, 'claude-sonnet-4', 'opus'])
  })
})
