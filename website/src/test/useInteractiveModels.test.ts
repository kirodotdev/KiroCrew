import { describe, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../api/client'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { effortToCarry, filterInteractiveModels, legacyCodexEffort, normalizeHiddenModels, shouldSeparateModelEffort, switchGroupedModel, useModelPickerConfigured, useModelPickerHiddenModelsQuery } from '../hooks/useInteractiveModels'

const MODELS = [
  { name: 'auto', description: '' },
  { name: 'model-a', description: 'A' },
  { name: 'model-b', description: 'B' },
]

describe('interactive model visibility', () => {
  it('shows newly advertised models and retains hidden choices across disappearance', () => {
    const hidden = ['model-b']
    const next = [...MODELS.filter(model => model.name !== 'model-b'), { name: 'new-model' }]
    expect(filterInteractiveModels(next, hidden).map(model => model.name)).toEqual(['auto', 'model-a', 'new-model'])
    expect(filterInteractiveModels([...next, MODELS[2]], hidden).map(model => model.name)).toEqual(['auto', 'model-a', 'new-model'])
    expect(filterInteractiveModels([...next, MODELS[2]], hidden, ['model-b']).map(model => model.name)).toContain('model-b')
  })

  it('hides the first-use row while loading and follows only the server acknowledgement', async () => {
    let finish!: (value: { model_picker_configured: boolean }) => void
    const response = new Promise<{ model_picker_configured: boolean }>(resolve => { finish = resolve })
    const request = vi.spyOn(api, 'dashboardConfig').mockReturnValue(response)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = renderHook(() => useModelPickerConfigured(), {
      wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children),
    })
    expect(view.result.current).toBe(true)
    await act(async () => { finish({ model_picker_configured: false }); await response })
    await waitFor(() => expect(view.result.current).toBe(false))
    act(() => client.setQueryData(['dashboardConfig'], { model_picker_configured: true, model_picker_hidden_models: [] }))
    await waitFor(() => expect(view.result.current).toBe(true))
    act(() => client.setQueryData(['dashboardConfig'], {}))
    await waitFor(() => expect(view.result.current).toBe(true))
    view.unmount()
    client.clear()
    request.mockRestore()
  })

  it('exposes a failed visibility-config read instead of silently treating it as success', async () => {
    const request = vi.spyOn(api, 'dashboardConfig').mockRejectedValue(new Error('offline'))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = renderHook(() => useModelPickerHiddenModelsQuery(), {
      wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children),
    })
    await waitFor(() => expect(view.result.current.isError).toBe(true))
    expect(view.result.current.data).toEqual([])
    view.unmount()
    client.clear()
    request.mockRestore()
  })

  it('shows the full list when the hidden setting is absent or empty', () => {
    expect(filterInteractiveModels(MODELS, []).map(model => model.name)).toEqual(['auto', 'model-a', 'model-b'])
    expect(normalizeHiddenModels(undefined)).toEqual([])
  })

  it('filters hidden models but always keeps auto and the active model', () => {
    expect(filterInteractiveModels(MODELS, ['auto', 'model-a', 'model-b'], ['model-b']).map(model => model.name))
      .toEqual(['auto', 'model-b'])
  })

  it('offers one Codex model row per base model when effort variants are advertised', () => {
    const codexModels = [
      { name: 'gpt-6-sol[low]', description: 'Fast' },
      { name: 'gpt-6-sol[medium]', description: 'Balanced' },
      { name: 'gpt-6-sol[high]', description: 'Deep' },
      { name: 'gpt-6-astra[max]', description: 'Flagship' },
      { name: 'claude-opus-4.8[1m]', description: 'Long context' },
    ]
    expect(filterInteractiveModels(codexModels, [], [], true).map(model => model.name))
      .toEqual(['gpt-6-sol', 'gpt-6-astra', 'claude-opus-4.8[1m]'])
  })

  it('uses an advertised base model for its own description and price', () => {
    const models = [
      { name: 'gpt-6-sol[low]', description: 'Low effort', rateMultiplier: 0.5 },
      { name: 'gpt-6-sol', description: 'Workhorse model', rateMultiplier: 1 },
    ]
    expect(filterInteractiveModels(models, [], [], true)).toEqual([
      { name: 'gpt-6-sol', description: 'Workhorse model', rateMultiplier: 1 },
    ])
  })

  it('keeps a description shared by all effort variants without a base row', () => {
    const models = [
      { name: 'gpt-6-astra[medium]', description: 'Frontier reasoning', rateMultiplier: 1.5 },
      { name: 'gpt-6-astra[high]', description: 'Frontier reasoning', rateMultiplier: 2 },
      { name: 'gpt-6-sol[low]', description: 'Fast responses' },
      { name: 'gpt-6-sol[high]', description: 'Deep reasoning' },
    ]
    expect(filterInteractiveModels(models, [], [], true)).toEqual([
      { name: 'gpt-6-astra', description: 'Frontier reasoning', rateMultiplier: undefined },
      { name: 'gpt-6-sol', description: '', rateMultiplier: undefined },
    ])
  })

  it('separates effort only when the capability response says IDs encode effort', () => {
    const pairModels = [{ name: 'gpt-6-sol[medium]' }]
    expect(shouldSeparateModelEffort(true, pairModels)).toBe(true)
    expect(shouldSeparateModelEffort(false, pairModels)).toBe(false)
    expect(shouldSeparateModelEffort(undefined, pairModels)).toBe(false)
    expect(shouldSeparateModelEffort(true, [{ name: 'auto' }])).toBe(false)
  })

  it('preserves a legacy Codex pair level only until separate effort is set', () => {
    expect(legacyCodexEffort('gpt-6-sol[max]', '', true)).toBe('max')
    expect(legacyCodexEffort('gpt-6-sol[max]', 'high', true)).toBe('')
    expect(legacyCodexEffort('gpt-6-sol[max]', '', false)).toBe('')
    expect(legacyCodexEffort('claude-opus-4.8[1m]', '', true)).toBe('')
  })

  it('carries a staged effort pick over the store and over a legacy pair level', () => {
    // The store lags the slider: a pick inside its debounce is only staged,
    // and a model pick in that window must not migrate the stale pair level
    // back over the user's choice. An effort already on the wire is NOT a
    // carry (it is waited for instead -- see the in-flight test below).
    expect(effortToCarry('gpt-6-sol[max]', '', 'high', false, true)).toBe('high')
    expect(effortToCarry('gpt-6-sol[max]', '', '', false, true)).toBe('')
    expect(effortToCarry('gpt-6-sol[max]', '', null, false, true)).toBe('max')
    expect(effortToCarry('gpt-6-sol[max]', 'high', null, false, true)).toBeNull()
    expect(effortToCarry('gpt-6-sol', '', null, false, true)).toBeNull()
    expect(effortToCarry('gpt-6-sol[max]', '', 'low', false, false)).toBe('low')
  })

  it('does not migrate a legacy pair level over an effort write already in flight', () => {
    // The slider's stage clears the moment its wire call begins, and the
    // store reads '' until that write lands -- exactly the state the legacy
    // branch matches. Migrating `max` there would queue it behind the user's
    // `high` and win the chain, silently reverting the pick.
    expect(effortToCarry('gpt-6-sol[max]', '', null, true, true)).toBeNull()
    // A stage made after the in-flight write still carries over it.
    expect(effortToCarry('gpt-6-sol[max]', '', 'low', true, true)).toBe('low')
  })

  it('registers the model pick at once and sends it only after the carried effort', async () => {
    const calls: string[] = []
    let releaseEffort!: () => void
    const effortWire = new Promise<void>(resolve => { releaseEffort = resolve })
    const run = switchGroupedModel('', async level => {
      calls.push(`effort-begin:${JSON.stringify(level)}`)
      await effortWire
      calls.push('effort-done')
    }, async afterEffort => {
      // The model switch takes its ticket NOW -- before the effort settles --
      // and defers only its wire send behind the effort write.
      calls.push('model-registered')
      await afterEffort
      calls.push('model-sent')
    })
    expect(calls).toEqual(['effort-begin:""', 'model-registered'])
    releaseEffort()
    await run
    expect(calls).toEqual(['effort-begin:""', 'model-registered', 'effort-done', 'model-sent'])

    calls.length = 0
    await switchGroupedModel(null, async level => { calls.push(`effort:${level}`) }, async afterEffort => { await afterEffort; calls.push('model') })
    expect(calls).toEqual(['model'])
  })

  it('waits for an in-flight effort verdict instead of re-sending it, and aborts on its refusal', async () => {
    // A level already on the wire must not be written twice: the repeat would
    // queue behind the original and burn its own confirm budget waiting. The
    // model pick waits on the original's verdict; a refusal aborts the pick
    // exactly as a refused carried write does.
    const calls: string[] = []
    let settle!: () => void
    const verdict = new Promise<void>(resolve => { settle = resolve })
    const run = switchGroupedModel(null, async level => { calls.push(`effort:${level}`) }, async afterEffort => {
      calls.push('model-registered')
      await afterEffort
      calls.push('model-sent')
    }, () => verdict)
    await Promise.resolve()
    expect(calls).toEqual(['model-registered'])
    settle()
    await run
    expect(calls).toEqual(['model-registered', 'model-sent'])

    const sent = vi.fn()
    await expect(switchGroupedModel(null, async () => {}, async afterEffort => {
      await afterEffort
      sent()
    }, () => Promise.reject(new Error('effort refused')))).rejects.toThrow('effort refused')
    expect(sent).not.toHaveBeenCalled()
  })

  it('waits on the carried write\'s wire verdict, not on its caller budget', async () => {
    // persistEffort resolves or rejects on the CALLER's confirm budget; the
    // wire call outlives it. Read after the write registered, the verdict is
    // what the model send waits on -- so an effort released unconfirmed
    // defers the model POST until the wire settles instead of cancelling it.
    const calls: string[] = []
    let settle!: () => void
    const verdict = new Promise<void>(resolve => { settle = resolve })
    const run = switchGroupedModel('high', async () => { throw new Error('not confirmed') }, async afterEffort => {
      calls.push('model-registered')
      await afterEffort
      calls.push('model-sent')
    }, () => verdict)
    // The caller still hears about the unconfirmed effort.
    await expect(run).rejects.toThrow('not confirmed')
    expect(calls).toEqual(['model-registered'])
    settle()
    await verdict
    await Promise.resolve()
    expect(calls).toEqual(['model-registered', 'model-sent'])
  })

  it('a refused effort write aborts the model send it was carried by', async () => {
    const sent = vi.fn()
    await expect(switchGroupedModel('max', async () => { throw new Error('effort refused') }, async afterEffort => {
      await afterEffort
      sent()
    })).rejects.toThrow('effort refused')
    expect(sent).not.toHaveBeenCalled()
  })

  it('a newer pick made during the older effort write keeps the higher model ticket', async () => {
    // Regression for the ordering race: with the model registered only after
    // its effort settled, pick B (no effort) registered before pick A's
    // model and A's later registration overwrote B. Registration order must
    // equal click order regardless of how long each carried effort takes.
    const registered: string[] = []
    let releaseA!: () => void
    const effortA = new Promise<void>(resolve => { releaseA = resolve })
    const pickA = switchGroupedModel('high', async () => { await effortA }, async afterEffort => {
      registered.push('A')
      await afterEffort
    })
    const pickB = switchGroupedModel(null, async () => {}, async afterEffort => {
      registered.push('B')
      await afterEffort
    })
    expect(registered).toEqual(['A', 'B'])
    releaseA()
    await Promise.all([pickA, pickB])
  })

  it('trims, deduplicates, and ignores invalid config entries', () => {
    expect(normalizeHiddenModels([' model-a ', 'model-a', '', 'auto', 3])).toEqual(['model-a'])
  })

  it('is wired only into ChatPage and ChatPane consumers', () => {
    const root = resolve(process.cwd(), 'src')
    const chatPage = readFileSync(resolve(root, 'pages/ChatPage.tsx'), 'utf8')
    const chatPane = readFileSync(resolve(root, 'components/ChatPane.tsx'), 'utf8')
    const bulkSwitcher = readFileSync(resolve(root, 'pages/ChatSidebar.tsx'), 'utf8')
    const settings = readFileSync(resolve(root, 'pages/settings/ChatPanel.tsx'), 'utf8')
    expect(chatPage).toContain('const availableModels = effectiveModels')
    expect(chatPage).toContain('useFilteredDropdown(modelPickerModels)')
    expect(chatPage).toContain('filterInteractiveModels(effectiveModels')
    expect(chatPage).toContain('modelVisibilityError={hiddenModelsQ.isError}')
    expect(chatPage).toContain('onRetryModelVisibility={() => hiddenModelsQ.refetch()}')
    expect(chatPane).toContain('const availableModels = effectiveModels')
    expect(chatPane).toContain('useFilteredDropdown(modelPickerModels)')
    expect(chatPane).toContain('filterInteractiveModels(effectiveModels')
    expect(chatPane).toContain('{hiddenModelsQ.isError && (')
    expect(chatPane).toContain('onClick={() => hiddenModelsQ.refetch()}')
    expect(bulkSwitcher).not.toContain('filterInteractiveModels(')
    expect(settings).not.toContain('filterInteractiveModels(')
  })
})
