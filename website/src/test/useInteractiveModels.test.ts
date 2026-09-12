import { describe, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../api/client'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { filterInteractiveModels, normalizeHiddenModels, useModelPickerConfigured, useModelPickerHiddenModelsQuery } from '../hooks/useInteractiveModels'

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
