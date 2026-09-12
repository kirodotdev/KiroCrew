vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))
vi.mock('@radix-ui/react-popover', async () => await import('./__mocks__/@radix-ui/react-popover'))

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'

const { dashboardConfigMock, modelsMock, updateDashboardConfigMock } = vi.hoisted(() => ({
  dashboardConfigMock: vi.fn(),
  modelsMock: vi.fn(),
  updateDashboardConfigMock: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: dashboardConfigMock,
    updateDashboardConfig: updateDashboardConfigMock,
    kirocrewConfig: () => Promise.resolve({ agent: { model: 'auto', reasoning_effort: '' } }),
    models: modelsMock,
    patchConfig: () => Promise.resolve({}),
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
    featureVideoStatus: () => Promise.resolve({ enabled: false }),
    featureVideoFetchAll: () => Promise.resolve({ ok: true }),
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'
import { createTestStore } from './helpers'

type DashboardConfigMock = {
  model_picker_hidden_models: string[]
  model_picker_configured: boolean
  [key: string]: unknown
}

function defaultDashboardConfig(): DashboardConfigMock {
  return {
    restore_sessions: false,
    restore_window_minutes: 30,
    merge_queued_messages: false,
    default_memory_mode: 'persistent',
    widget_density: 'more',
    verbosity: 'default',
    quick_send: false,
    session_grid: false,
    tail_fork_enabled: false,
    link_previews: false,
    mcp_app_panel: false,
    auto_open_git_panel: false,
    session_card_source_links: true,
    folder_suggestions_enabled: true,
    use_builtin_browser: true,
    model_picker_hidden_models: [],
    model_picker_configured: false,
  }
}

function applyDashboardConfigPatch(config: DashboardConfigMock, patch: Record<string, unknown>): DashboardConfigMock {
  const { model_picker_hidden_models_add, model_picker_hidden_models_remove, ...rest } = patch
  const next = { ...config, ...rest }
  if ('model_picker_hidden_models' in patch) next.model_picker_configured = true
  if (Array.isArray(model_picker_hidden_models_add) || Array.isArray(model_picker_hidden_models_remove)) {
    const remove = new Set((Array.isArray(model_picker_hidden_models_remove) ? model_picker_hidden_models_remove : []) as string[])
    const merged = config.model_picker_hidden_models.filter(model => !remove.has(model))
    const seen = new Set(merged)
    for (const model of (Array.isArray(model_picker_hidden_models_add) ? model_picker_hidden_models_add : []) as string[]) {
      if (!seen.has(model)) {
        seen.add(model)
        merged.push(model)
      }
    }
    next.model_picker_hidden_models = merged
    next.model_picker_configured = true
  }
  return next
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return Object.assign(render(
    <Provider store={createTestStore()}>
      <QueryClientProvider client={client}><ChatPanel /></QueryClientProvider>
    </Provider>,
  ), { client })
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

beforeEach(() => {
  let serverConfig = defaultDashboardConfig()
  dashboardConfigMock.mockReset().mockImplementation(async () => serverConfig)
  modelsMock.mockReset().mockResolvedValue([
    { model_name: 'auto', description: 'Default' },
    { model_name: 'model-a', description: 'Model A' },
    { model_name: 'model-b', description: 'Model B' },
  ])
  updateDashboardConfigMock.mockReset().mockImplementation(async (patch: Record<string, unknown>) => {
    serverConfig = applyDashboardConfigPatch(serverConfig, patch)
    return { ok: true }
  })
})

describe('Settings selectable models', () => {
  it('reports a catalog failure and enables visibility controls after retry', async () => {
    modelsMock
      .mockRejectedValueOnce(new Error('catalog unavailable'))
      .mockResolvedValueOnce([
        { model_name: 'auto', description: 'Default' },
        { model_name: 'model-a', description: 'Model A' },
        { model_name: 'model-b', description: 'Model B' },
      ])
    mount()
    const trigger = await screen.findByRole('button', { name: 'Selectable Models' })
    expect(await screen.findByRole('alert')).toHaveTextContent('Failed to load config.')
    expect(trigger).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(modelsMock).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(trigger).not.toBeDisabled())
    expect(screen.queryByText('Failed to load config.')).not.toBeInTheDocument()
  })

  it('keeps bulk visibility saves disabled until the model catalog loads', async () => {
    const models = deferred<Array<{ model_name: string; description: string }>>()
    modelsMock.mockReturnValueOnce(models.promise)
    mount()
    const trigger = await screen.findByRole('button', { name: 'Selectable Models' })
    await waitFor(() => expect(dashboardConfigMock).toHaveBeenCalled())
    expect(trigger).toBeDisabled()
    fireEvent.click(trigger)
    expect(screen.queryByRole('button', { name: 'Deselect all' })).not.toBeInTheDocument()
    expect(updateDashboardConfigMock).not.toHaveBeenCalled()

    models.resolve([
      { model_name: 'auto', description: 'Default' },
      { model_name: 'model-a', description: 'Model A' },
      { model_name: 'model-b', description: 'Model B' },
    ])
    await waitFor(() => expect(trigger).not.toBeDisabled())
    fireEvent.click(trigger)
    fireEvent.click(screen.getByRole('button', { name: 'Deselect all' }))
    await waitFor(() => expect(updateDashboardConfigMock).toHaveBeenCalledWith({
      model_picker_hidden_models_add: ['model-a', 'model-b'],
    }))
  })

  it('does not acknowledge visits or failed saves, and keeps successful acknowledgement after restoring all', async () => {
    let serverConfig = await dashboardConfigMock()
    dashboardConfigMock.mockImplementation(async () => serverConfig)
    updateDashboardConfigMock.mockRejectedValueOnce(new Error('write failed')).mockImplementation(async patch => {
      serverConfig = applyDashboardConfigPatch(serverConfig, patch)
      return { ok: true }
    })
    const { client } = mount()
    const trigger = await screen.findByRole('button', { name: 'Selectable Models' })
    await waitFor(() => expect(trigger).toHaveTextContent('All models (3)'))
    const configured = () => client.getQueryData<{ model_picker_configured: boolean }>(['dashboardConfig'])?.model_picker_configured
    expect(configured()).toBe(false)
    expect(updateDashboardConfigMock).not.toHaveBeenCalled()
    fireEvent.click(trigger)
    fireEvent.click(screen.getByRole('checkbox', { name: 'model-a' }))
    expect(await screen.findByText('Failed to save selectable models')).toBeInTheDocument()
    expect(configured()).toBe(false)
    fireEvent.click(screen.getByRole('checkbox', { name: 'model-a' }))
    await waitFor(() => expect(configured()).toBe(true))
    fireEvent.click(screen.getByRole('checkbox', { name: 'model-a' }))
    await waitFor(() => expect(updateDashboardConfigMock).toHaveBeenLastCalledWith({ model_picker_hidden_models_remove: ['model-a'] }))
    expect(configured()).toBe(true)
    for (const [body] of updateDashboardConfigMock.mock.calls) expect(body).not.toHaveProperty('model_picker_configured')
  })

  it('preserves hidden IDs absent from the advertised model list', async () => {
    const config = await dashboardConfigMock()
    dashboardConfigMock.mockResolvedValue({ ...config, model_picker_hidden_models: ['temporarily-unavailable'] })
    mount()
    const trigger = await screen.findByRole('button', { name: 'Selectable Models' })
    await waitFor(() => expect(trigger).toHaveTextContent('All models (3)'))
    fireEvent.click(trigger)
    expect(screen.queryByRole('checkbox', { name: 'temporarily-unavailable' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: 'model-a' }))
    await waitFor(() => expect(updateDashboardConfigMock).toHaveBeenCalledWith({ model_picker_hidden_models_add: ['model-a'] }))
  })

  it('omits model visibility and acknowledgement from unrelated dashboard saves', async () => {
    mount()
    const quickSend = await screen.findByRole('switch', { name: 'Quick Send' })
    await waitFor(() => expect(quickSend).not.toBeDisabled())
    fireEvent.click(quickSend)
    await waitFor(() => expect(updateDashboardConfigMock).toHaveBeenCalledWith({ quick_send: true }))
  })

  it('defaults to all selected, searches, persists hidden IDs, and locks auto', async () => {
    mount()
    const trigger = await screen.findByRole('button', { name: 'Selectable Models' })
    await waitFor(() => expect(trigger).toHaveTextContent('All models (3)'))
    fireEvent.click(trigger)

    const auto = screen.getByRole('checkbox', { name: 'auto' })
    expect(auto).toBeChecked()
    expect(auto).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: 'model-a' })).toBeChecked()
    expect(auto.closest('label')).toHaveClass('min-h-11')

    fireEvent.change(screen.getByRole('textbox', { name: 'Search models…' }), { target: { value: 'model-b' } })
    expect(screen.getByRole('checkbox', { name: 'model-b' })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'model-a' })).toBeNull()

    fireEvent.change(screen.getByRole('textbox', { name: 'Search models…' }), { target: { value: '' } })
    fireEvent.click(screen.getByRole('checkbox', { name: 'model-a' }))
    await waitFor(() => expect(updateDashboardConfigMock).toHaveBeenCalledWith({
      model_picker_hidden_models_add: ['model-a'],
    }))
    expect(trigger).toHaveTextContent('Selected 2 / 3')
  })

  it('moves from search through options with arrow keys and toggles the focused row', async () => {
    mount()
    const trigger = await screen.findByRole('button', { name: 'Selectable Models' })
    await waitFor(() => expect(trigger).toHaveTextContent('All models (3)'))
    fireEvent.click(trigger)
    const search = screen.getByRole('textbox', { name: 'Search models…' })
    search.focus()
    fireEvent.keyDown(search, { key: 'ArrowDown' })
    const autoRow = screen.getByRole('checkbox', { name: 'auto' }).closest('label') as HTMLElement
    expect(autoRow).toHaveFocus()
    fireEvent.keyDown(autoRow, { key: 'ArrowDown' })
    const modelARow = screen.getByRole('checkbox', { name: 'model-a' }).closest('label') as HTMLElement
    expect(modelARow).toHaveFocus()
    fireEvent.keyDown(modelARow, { key: ' ' })
    await waitFor(() => expect(updateDashboardConfigMock).toHaveBeenCalledWith({
      model_picker_hidden_models_add: ['model-a'],
    }))
  })

  it('bulk-selects the advertised catalog atomically while preserving absent hidden IDs', async () => {
    let serverConfig = {
      ...await dashboardConfigMock(),
      model_picker_hidden_models: ['temporarily-unavailable', 'model-a'],
    }
    dashboardConfigMock.mockImplementation(async () => serverConfig)
    updateDashboardConfigMock.mockImplementation(async patch => {
      serverConfig = applyDashboardConfigPatch(serverConfig, patch)
      return { ok: true }
    })
    const user = userEvent.setup()
    mount()
    const trigger = await screen.findByRole('button', { name: 'Selectable Models' })
    await waitFor(() => expect(trigger).toHaveTextContent('Selected 2 / 3'))
    await user.click(trigger)

    const search = screen.getByRole('textbox', { name: 'Search models…' })
    expect(search).toHaveFocus()
    await user.tab()
    const selectAll = screen.getByRole('button', { name: 'Select all' })
    expect(selectAll).toHaveFocus()
    await user.keyboard('{Enter}')

    await waitFor(() => expect(updateDashboardConfigMock).toHaveBeenCalledTimes(1))
    expect(updateDashboardConfigMock).toHaveBeenLastCalledWith({
      model_picker_hidden_models_remove: ['model-a', 'model-b'],
    })
    await waitFor(() => expect(screen.getByRole('checkbox', { name: 'model-a' })).toBeChecked())
    expect(screen.getByRole('checkbox', { name: 'model-b' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'auto' })).toBeChecked()
  })

  it('deselects optional advertised models in one save and acknowledges only success', async () => {
    let serverConfig = {
      ...await dashboardConfigMock(),
      model_picker_hidden_models: ['temporarily-unavailable'],
    }
    dashboardConfigMock.mockImplementation(async () => serverConfig)
    updateDashboardConfigMock.mockRejectedValueOnce(new Error('write failed')).mockImplementationOnce(async patch => {
      serverConfig = applyDashboardConfigPatch(serverConfig, patch)
      return { ok: true }
    })
    const user = userEvent.setup()
    const { client } = mount()
    const trigger = await screen.findByRole('button', { name: 'Selectable Models' })
    await waitFor(() => expect(trigger).toHaveTextContent('All models (3)'))
    await user.click(trigger)

    const configured = () => client.getQueryData<{ model_picker_configured: boolean }>(['dashboardConfig'])?.model_picker_configured
    await user.click(screen.getByRole('button', { name: 'Deselect all' }))
    await waitFor(() => expect(updateDashboardConfigMock).toHaveBeenCalledTimes(1))
    expect(updateDashboardConfigMock).toHaveBeenLastCalledWith({
      model_picker_hidden_models_add: ['model-a', 'model-b'],
    })
    expect(await screen.findByText('Failed to save selectable models')).toBeInTheDocument()
    expect(configured()).toBe(false)
    expect(screen.getByRole('checkbox', { name: 'auto' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'auto' })).toBeDisabled()

    await user.click(screen.getByRole('button', { name: 'Deselect all' }))
    await waitFor(() => expect(updateDashboardConfigMock).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(configured()).toBe(true))
    expect(updateDashboardConfigMock).toHaveBeenLastCalledWith({
      model_picker_hidden_models_add: ['model-a', 'model-b'],
    })
  })

  it('rolls the edited selection back when persistence fails', async () => {
    updateDashboardConfigMock.mockRejectedValueOnce(new Error('write failed'))
    mount()
    const trigger = await screen.findByRole('button', { name: 'Selectable Models' })
    await waitFor(() => expect(trigger).toHaveTextContent('All models (3)'))
    fireEvent.click(trigger)
    const modelB = screen.getByRole('checkbox', { name: 'model-b' })
    fireEvent.click(modelB)

    expect(await screen.findByText('Failed to save selectable models')).toBeInTheDocument()
    expect(modelB).toBeChecked()
    expect(trigger).toHaveTextContent('All models (3)')
  })

  it('serializes rapid replacements so the newest selection is the server final value', async () => {
    let serverConfig = await dashboardConfigMock()
    dashboardConfigMock.mockImplementation(async () => serverConfig)
    const first = deferred<{ ok: true }>()
    const second = deferred<{ ok: true }>()
    updateDashboardConfigMock
      .mockImplementationOnce(async patch => {
        const result = await first.promise
        serverConfig = applyDashboardConfigPatch(serverConfig, patch)
        return result
      })
      .mockImplementationOnce(async patch => {
        const result = await second.promise
        serverConfig = applyDashboardConfigPatch(serverConfig, patch)
        return result
      })
    mount()
    const trigger = await screen.findByRole('button', { name: 'Selectable Models' })
    await waitFor(() => expect(trigger).toHaveTextContent('All models (3)'))
    fireEvent.click(trigger)
    fireEvent.click(screen.getByRole('checkbox', { name: 'model-a' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'model-b' }))
    expect(trigger).toHaveTextContent('Selected 1 / 3')
    await waitFor(() => expect(updateDashboardConfigMock).toHaveBeenCalledTimes(1))

    first.resolve({ ok: true })
    await waitFor(() => expect(updateDashboardConfigMock).toHaveBeenCalledTimes(2))
    expect(updateDashboardConfigMock).toHaveBeenLastCalledWith({
      model_picker_hidden_models_add: ['model-b'],
    })
    second.resolve({ ok: true })
    await waitFor(() => expect(trigger).toHaveTextContent('Selected 1 / 3'))
    expect(trigger).toHaveTextContent('Selected 1 / 3')
  })
})
