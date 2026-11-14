import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

vi.mock('../../api/client', () => ({
  api: {
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
    // Global invalidation also refetches the providers' theme queries.
    themes: vi.fn().mockResolvedValue({ themes: [] }),
    themeBoot: vi.fn().mockResolvedValue(null),
  },
}))

import { api } from '../../api/client'
import { AllSettingsList, valueAt } from './AllSettingsList'

// Exercise the real select's native path, including its busy guard.
vi.mock('../../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => true }))

const ENTRIES = [
  {
    path: 'mcp_gateway.keep_alive_servers', type: 'array', label: 'Keep-alive servers',
    help: 'Servers to keep running.', defaultValue: [],
    allSettings: { mode: 'edit', control: 'str_list', resettable: true },
  },
  {
    path: 'mcp_gateway.idle_timeout_secs', type: 'integer', label: 'Idle timeout',
    help: '', defaultValue: 300, requiresRestart: true,
    allSettings: { mode: 'edit', control: 'int', resettable: true, min: 0, max: 1e12 },
  },
  {
    path: 'agent.streaming', type: 'boolean', label: 'Streaming',
    help: '', defaultValue: true,
    allSettings: { mode: 'edit', control: 'bool', resettable: true },
  },
  {
    path: 'power.on_battery', type: 'string', label: 'On battery',
    help: '', defaultValue: 'normal',
    allSettings: { mode: 'edit', control: 'enum', resettable: true, values: ['normal', 'reduced', 'paused'] },
  },
  {
    path: 'agent.dangerously_skip_permissions', type: 'boolean', label: 'Skip approvals',
    help: '', defaultValue: false, requiresRestart: true, allSettings: { mode: 'readonly', reason: 'safety' },
  },
  {
    path: 'agent.sandbox', type: 'string', label: 'Sandbox',
    help: '', defaultValue: 'auto', requiresRestart: true, allSettings: { mode: 'control', page: { tab: 'developer', sub: 'config' } },
  },
  // Not listed: the gateway sent no allSettings record.
  { path: 'agent.env', type: 'object', label: 'Agent env', help: '', defaultValue: {} },
]

const CONFIG = {
  mcp_gateway: { keep_alive_servers: ['github'], idle_timeout_secs: 300 },
  agent: { streaming: true, dangerously_skip_permissions: false },
  power: { on_battery: 'normal' },
}

function rowFor(key: string): HTMLElement {
  const row = document.querySelector<HTMLElement>(`[data-setting-key="${key}"]`)
  if (!row) throw new Error(`no row for ${key}`)
  return row
}

/** Click a row's Reset to default, then the inline prompt's Reset. */
function confirmReset(key: string) {
  const row = rowFor(key)
  fireEvent.click(within(row).getByRole('button', { name: new RegExp(`Reset ${key}`) }))
  fireEvent.click(within(row).getByRole('button', { name: 'Reset' }))
}

describe('AllSettingsList', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ entries: ENTRIES }), { status: 200 })))
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue(CONFIG)
    ;(api.patchConfig as ReturnType<typeof vi.fn>).mockResolvedValue(CONFIG)
  })
  afterEach(() => vi.unstubAllGlobals())

  it('lists only keys the gateway marks, with the value and default', async () => {
    renderWithProviders(<AllSettingsList />)
    await screen.findByText('Keep-alive servers')
    expect(screen.queryByText('Agent env')).toBeNull()
    // A restart key names what restarts once, in its badge; the status line keeps its own words.
    expect(within(rowFor('mcp_gateway.idle_timeout_secs')).getByText('Applies after gateway restart')).toBeTruthy()
    expect(within(rowFor('mcp_gateway.idle_timeout_secs')).getByText('Saves when you leave the field')).toBeTruthy()
    expect(within(rowFor('agent.dangerously_skip_permissions')).getByText('Read only')).toBeTruthy()
    expect(within(rowFor('agent.dangerously_skip_permissions')).queryByRole('switch')).toBeNull()
  })

  it('saves a list when it loses focus, one item per line, like every other field', async () => {
    renderWithProviders(<AllSettingsList />)
    const box = await screen.findByLabelText('Keep-alive servers')
    // Leaving an unchanged list saves nothing.
    fireEvent.blur(box)
    expect(api.patchConfig).not.toHaveBeenCalled()
    fireEvent.change(box, { target: { value: 'github\n  slack \n\n' } })
    fireEvent.blur(box)
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('mcp_gateway.keep_alive_servers', ['github', 'slack'], undefined))
    await waitFor(() => expect(within(rowFor('mcp_gateway.keep_alive_servers')).getByText('Saved')).toBeTruthy())
  })

  it.each([
    ['save', 'successful'], ['save', 'refused'],
    ['reset', 'successful'], ['reset', 'refused'],
  ])('invalidates every cache after a %s that is %s', async (action, outcome) => {
    const tips = {
      path: 'dashboard.tips_enabled', type: 'boolean', label: 'Tips enabled',
      help: '', defaultValue: true,
      allSettings: { mode: 'edit', control: 'bool', resettable: true },
    }
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ entries: [tips] }), { status: 200 })))
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({ dashboard: { tips_enabled: false } })
    const { queryClient } = renderWithProviders(<AllSettingsList />, { queryDefaults: { staleTime: Infinity } })
    const toggle = await screen.findByRole('switch', { name: 'Tips enabled' })
    if (outcome === 'refused') {
      vi.mocked(api.patchConfig).mockRejectedValueOnce(new Error('save refused'))
    }
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    queryClient.setQueryData(['tipsStatus'], { enabled_config: false, opted_out: false })
    queryClient.setQueryData(['tipsAll'], { tips: [] })
    expect(queryClient.getQueryState(['tipsStatus'])?.isInvalidated).toBe(false)
    expect(queryClient.getQueryState(['tipsAll'])?.isInvalidated).toBe(false)

    if (action === 'save') fireEvent.click(toggle)
    else confirmReset('dashboard.tips_enabled')
    await waitFor(() => expect(api.patchConfig).toHaveBeenLastCalledWith(
      'dashboard.tips_enabled', action === 'reset' ? undefined : true,
      action === 'reset' ? { unset: true } : undefined,
    ))
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith())
    await waitFor(() => expect(queryClient.isMutating()).toBe(0))
    // Chat is unmounted while Advanced is open; its infinite-lived caches must
    // be stale so opening Chat again fetches the gateway's current answers.
    expect(queryClient.getQueryState(['tipsStatus'])?.isInvalidated).toBe(true)
    expect(queryClient.getQueryState(['tipsAll'])?.isInvalidated).toBe(true)
  })

  it('saves a number as a number and resets a changed key with unset', async () => {
    renderWithProviders(<AllSettingsList />)
    const field = await screen.findByLabelText('Idle timeout')
    fireEvent.change(field, { target: { value: '120' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('mcp_gateway.idle_timeout_secs', 120, undefined))

    confirmReset('mcp_gateway.keep_alive_servers')
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('mcp_gateway.keep_alive_servers', undefined, { unset: true }))
    // A key at its default still shows its reset, disabled, so Reset is visible before any change.
    const atDefault = within(rowFor('agent.streaming')).getByRole('button', { name: /Reset agent.streaming/ })
    expect((atDefault as HTMLButtonElement).disabled).toBe(true)
    expect(within(rowFor('agent.streaming')).getByText('Saves as soon as you change it')).toBeTruthy()
    // A read-only row labels its value and drops the restart badge.
    expect(within(rowFor('agent.dangerously_skip_permissions')).getByText('Current: Disabled')).toBeTruthy()
    expect(within(rowFor('agent.dangerously_skip_permissions')).queryByText('Applies after gateway restart')).toBeNull()
  })

  it('asks in the row before Reset to default, naming the current value', async () => {
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({ ...CONFIG, mcp_gateway: { ...CONFIG.mcp_gateway, keep_alive_servers: ['github', 'slack'] } })
    renderWithProviders(<AllSettingsList />)
    await screen.findByLabelText('Keep-alive servers')
    const row = rowFor('mcp_gateway.keep_alive_servers')
    const resetName = /Reset mcp_gateway.keep_alive_servers/
    fireEvent.click(within(row).getByRole('button', { name: resetName }))
    await act(async () => {})
    expect(api.patchConfig).not.toHaveBeenCalled()
    expect(within(row).getByText('Reset to default? Current: github, slack')).toBeTruthy()
    // Cancel takes focus when the prompt opens and gives it back to Reset to default.
    const cancel = within(row).getByRole('button', { name: 'Cancel' })
    expect(document.activeElement).toBe(cancel)
    fireEvent.click(cancel)
    expect(within(row).queryByText(/Reset to default\?/)).toBeNull()
    expect(document.activeElement).toBe(within(row).getByRole('button', { name: resetName }))
    // Escape closes it as well.
    fireEvent.click(within(row).getByRole('button', { name: resetName }))
    fireEvent.keyDown(within(row).getByRole('button', { name: 'Cancel' }), { key: 'Escape' })
    expect(within(row).queryByText(/Reset to default\?/)).toBeNull()
    await act(async () => {})
    expect(api.patchConfig).not.toHaveBeenCalled()

    fireEvent.click(within(row).getByRole('button', { name: resetName }))
    fireEvent.click(within(row).getByRole('button', { name: 'Reset' }))
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledTimes(1))
    expect(api.patchConfig).toHaveBeenCalledWith('mcp_gateway.keep_alive_servers', undefined, { unset: true })
    expect(within(row).queryByText(/Reset to default\?/)).toBeNull()
  })

  it('offers an enum key its own values and saves the one picked', async () => {
    renderWithProviders(<AllSettingsList />)
    const select = await screen.findByLabelText('On battery') as HTMLSelectElement
    expect([...select.options].map(o => o.value)).toEqual(['normal', 'reduced', 'paused'])
    expect(select.value).toBe('normal')
    fireEvent.change(select, { target: { value: 'reduced' } })
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('power.on_battery', 'reduced', undefined))
  })

  it('refuses toggle and Reset presses while a toggle save is pending', async () => {
    vi.mocked(api.patchConfig).mockResolvedValueOnce(CONFIG)
    const { queryClient } = renderWithProviders(<AllSettingsList />)
    const toggle = await screen.findByRole('switch', { name: 'Streaming' })
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ ...CONFIG, agent: { ...CONFIG.agent, streaming: false } })
    fireEvent.click(toggle)
    // The value now differs from its default, so Reset is enabled until the next save starts.
    const resetBefore = within(rowFor('agent.streaming')).getByRole('button', { name: /Reset agent.streaming/ }) as HTMLButtonElement
    await waitFor(() => expect(resetBefore.disabled).toBe(false))
    await waitFor(() => expect(queryClient.isMutating()).toBe(0))
    let finish!: (value: Record<string, unknown>) => void
    vi.mocked(api.patchConfig).mockClear().mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    fireEvent.click(toggle)
    await waitFor(() => expect(toggle.getAttribute('aria-disabled')).toBe('true'))
    try {
      const reset = within(rowFor('agent.streaming')).getByRole('button', { name: /Reset agent.streaming/ }) as HTMLButtonElement
      expect(reset.disabled).toBe(true)
      fireEvent.click(toggle)
      fireEvent.keyDown(toggle, { key: 'Enter' })
      fireEvent.click(reset)
      await act(async () => {})
      expect(api.patchConfig).toHaveBeenCalledTimes(1)
    } finally {
      await act(async () => { finish(CONFIG) })
      await waitFor(() => expect(queryClient.isMutating()).toBe(0))
    }
  })

  it.each(['save', 'reset'])('refuses a second enum choice during %s and its refetch', async action => {
    const patchConfig = vi.mocked(api.patchConfig)
    const kc = vi.mocked(api.kirocrewConfig)
    const reduced = { ...CONFIG, power: { on_battery: 'reduced' } }
    kc.mockResolvedValue(action === 'reset' ? reduced : CONFIG)
    const { queryClient } = renderWithProviders(<AllSettingsList />)
    await screen.findByLabelText('On battery')
    const row = rowFor('power.on_battery')
    patchConfig.mockClear()
    let finishPatch!: (value: Record<string, unknown>) => void
    let finishRead!: (value: Record<string, unknown>) => void
    patchConfig.mockImplementationOnce(() => new Promise(resolve => { finishPatch = resolve }))
    kc.mockImplementationOnce(() => new Promise(resolve => { finishRead = resolve }))
    const select = screen.getByLabelText('On battery')
    if (action === 'save') fireEvent.change(select, { target: { value: 'reduced' } })
    else confirmReset('power.on_battery')
    await waitFor(() => expect(patchConfig).toHaveBeenCalledTimes(1))
    const reset = within(row).getByRole('button', { name: /Reset power.on_battery/ }) as HTMLButtonElement
    await waitFor(() => expect(reset.disabled).toBe(true))
    try {
      fireEvent.change(select, { target: { value: 'paused' } })
      fireEvent.click(reset)
      await act(async () => {})
      expect(patchConfig).toHaveBeenCalledTimes(1)
      expect(select.getAttribute('aria-busy')).toBe('true')
      await act(async () => { finishPatch(CONFIG) })
      await waitFor(() => expect(finishRead).toBeTypeOf('function'))
      fireEvent.change(select, { target: { value: 'paused' } })
      await act(async () => {})
      expect(patchConfig).toHaveBeenCalledTimes(1)
      expect(select.getAttribute('aria-busy')).toBe('true')
    } finally {
      await act(async () => { finishPatch(CONFIG) })
      await waitFor(() => expect(finishRead).toBeTypeOf('function'))
      await act(async () => { finishRead(action === 'save' ? reduced : CONFIG) })
      await waitFor(() => expect(queryClient.isMutating()).toBe(0))
    }
    expect(screen.getByLabelText('On battery').getAttribute('aria-busy')).toBeNull()
  })

  it.each([
    ['int', 'mcp_gateway.idle_timeout_secs', 'Idle timeout', '120'],
    ['float', 'agent.spawn_min_memory_gb', 'Memory floor', '1.5'],
    ['str', 'timezone', 'Timezone', 'Europe/London'],
    ['str_list', 'mcp_gateway.keep_alive_servers', 'Keep-alive servers', 'github\nslack'],
  ])('refuses another blur save from a busy %s field', async (control, path, label, draft) => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ entries: [{
      path, label, defaultValue: control === 'str' ? '' : control === 'str_list' ? [] : 300,
      allSettings: { mode: 'edit', control, resettable: true },
    }] }), { status: 200 })))
    let finish!: (value: Record<string, unknown>) => void
    vi.mocked(api.patchConfig).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const { queryClient } = renderWithProviders(<AllSettingsList />)
    const field = await screen.findByLabelText(label) as HTMLInputElement | HTMLTextAreaElement
    fireEvent.change(field, { target: { value: draft } })
    if (control === 'str_list') fireEvent.blur(field)
    else fireEvent.keyDown(field, { key: 'Enter' })
    await waitFor(() => expect(field.disabled).toBe(true))
    try {
      // Disabling a focused input after Enter can deliver its blur after the save started.
      fireEvent.blur(field)
      await act(async () => {})
      expect(api.patchConfig).toHaveBeenCalledTimes(1)
    } finally {
      await act(async () => { finish(CONFIG) })
      await waitFor(() => expect(queryClient.isMutating()).toBe(0))
    }
  })

  it('marks a refused save as not saved and puts the saved value back in one click', async () => {
    ;(api.patchConfig as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('must be between 0 and 10'))
    renderWithProviders(<AllSettingsList />)
    const field = await screen.findByLabelText('Idle timeout')
    fireEvent.change(field, { target: { value: '-5' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    const row = rowFor('mcp_gateway.idle_timeout_secs')
    await within(row).findByText('Not saved')
    expect(field.getAttribute('aria-invalid')).toBe('true')
    // A save status would read as if this value had saved.
    expect(within(row).queryByRole('status')).toBeNull()
    fireEvent.click(within(row).getByRole('button', { name: 'Discard my edit' }))
    await waitFor(() => expect((screen.getByLabelText('Idle timeout') as HTMLInputElement).value).toBe('300'))
    expect(within(row).queryByText('Not saved')).toBeNull()
  })

  it('shows a live save status on each editable row', async () => {
    renderWithProviders(<AllSettingsList />)
    const field = await screen.findByLabelText('Idle timeout')
    const row = rowFor('mcp_gateway.idle_timeout_secs')
    // Before any save: what will make it save.
    expect(within(row).getByRole('status').textContent).toBe('Saves when you leave the field')
    expect(within(rowFor('mcp_gateway.keep_alive_servers')).getByRole('status').textContent).toBe('Saves when you leave the field')
    expect(within(rowFor('agent.streaming')).getByRole('status').textContent).toBe('Saves as soon as you change it')
    expect(within(rowFor('power.on_battery')).getByRole('status').textContent).toBe('Saves as soon as you change it')
    // A read-only or linked row has no status.
    expect(within(rowFor('agent.dangerously_skip_permissions')).queryByRole('status')).toBeNull()
    expect(within(rowFor('agent.sandbox')).queryByRole('status')).toBeNull()

    let finish!: (value: Record<string, unknown>) => void
    vi.mocked(api.patchConfig).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    fireEvent.change(field, { target: { value: '120' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    const status = within(row).getByRole('status')
    expect(status.getAttribute('aria-live')).toBe('polite')
    await waitFor(() => expect(status.textContent).toBe('Saving…'))
    await act(async () => { finish(CONFIG) })
    await waitFor(() => expect(status.textContent).toBe('Saved'))
  })

  it('shows Saved after a toggle save and Saving… while a Reset runs', async () => {
    const { queryClient } = renderWithProviders(<AllSettingsList />)
    const toggle = await screen.findByRole('switch', { name: 'Streaming' })
    const row = rowFor('agent.streaming')
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ ...CONFIG, agent: { ...CONFIG.agent, streaming: false } })
    fireEvent.click(toggle)
    await waitFor(() => expect(within(row).getByRole('status').textContent).toBe('Saved'))
    let finish!: (value: Record<string, unknown>) => void
    vi.mocked(api.patchConfig).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    confirmReset('agent.streaming')
    await waitFor(() => expect(within(row).getByRole('status').textContent).toBe('Saving…'))
    await act(async () => { finish(CONFIG) })
    await waitFor(() => expect(queryClient.isMutating()).toBe(0))
    await waitFor(() => expect(within(row).getByRole('status').textContent).toBe('Saved'))
  })

  it('shows no save status beside a refused save', async () => {
    vi.mocked(api.patchConfig).mockRejectedValueOnce(new Error('must be between 0 and 10'))
    renderWithProviders(<AllSettingsList />)
    const field = await screen.findByLabelText('Idle timeout')
    fireEvent.change(field, { target: { value: '-5' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    const row = rowFor('mcp_gateway.idle_timeout_secs')
    await within(row).findByText('Not saved')
    for (const text of ['Saves when you leave the field', 'Saving…', 'Saved']) {
      expect(within(row).queryByText(text)).toBeNull()
    }
  })

  it('shows what the gateway holds after a save the loader clamped back to the old value', async () => {
    // The gateway accepts 2000 but its loader clamps the field, so the reread value is still 300.
    renderWithProviders(<AllSettingsList />)
    const field = await screen.findByLabelText('Idle timeout')
    fireEvent.change(field, { target: { value: '2000' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('mcp_gateway.idle_timeout_secs', 2000, undefined))
    await waitFor(() => expect((screen.getByLabelText('Idle timeout') as HTMLInputElement).value).toBe('300'))
  })

  it('says in plain words why a read-only row cannot change here', async () => {
    renderWithProviders(<AllSettingsList />)
    const row = await waitFor(() => rowFor('agent.dangerously_skip_permissions'))
    expect(within(row).getByText('Read only')).toBeTruthy()
    expect(within(row).getByText('Read only: it decides what the agent may run, or do without asking you.')).toBeTruthy()
  })

  it('offers no Undo after a successful save or reset', async () => {
    const kc = api.kirocrewConfig as ReturnType<typeof vi.fn>
    const { queryClient } = renderWithProviders(<AllSettingsList />)
    const box = await screen.findByLabelText('Keep-alive servers')
    const row = rowFor('mcp_gateway.keep_alive_servers')
    kc.mockResolvedValue({ ...CONFIG, mcp_gateway: { ...CONFIG.mcp_gateway, keep_alive_servers: ['github', 'slack'] } })
    fireEvent.change(box, { target: { value: 'github\nslack' } })
    fireEvent.blur(box)
    // The replacement field proves onSuccess finished.
    await waitFor(() => expect(box.isConnected).toBe(false))
    expect(within(row).queryByRole('button', { name: /Undo|Discard my edit/ })).toBeNull()

    confirmReset('mcp_gateway.keep_alive_servers')
    await waitFor(() => expect(api.patchConfig).toHaveBeenLastCalledWith('mcp_gateway.keep_alive_servers', undefined, { unset: true }))
    await waitFor(() => expect(queryClient.isMutating()).toBe(0))
    expect(within(row).queryByRole('button', { name: /Undo|Discard my edit/ })).toBeNull()
  })

  it('shows the default of an edit key the config view leaves out, with Reset disabled', async () => {
    // dashboard.terminal's stored dict has no max_sessions; the terminal code reads 12 when it is absent.
    const terminal = {
      path: 'dashboard.terminal.max_sessions', type: 'integer', label: 'Max terminals',
      help: '', defaultValue: 12,
      allSettings: { mode: 'edit', control: 'int', resettable: true, min: 0, max: 1e12 },
    }
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ entries: [...ENTRIES, terminal] }), { status: 200 })))
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({ ...CONFIG, dashboard: { terminal: { enabled: true } } })
    renderWithProviders(<AllSettingsList />)
    const field = await screen.findByLabelText('Max terminals') as HTMLInputElement
    expect(field.value).toBe('12')
    const reset = within(rowFor('dashboard.terminal.max_sessions')).getByRole('button', { name: /Reset dashboard.terminal.max_sessions/ })
    expect((reset as HTMLButtonElement).disabled).toBe(true)
  })

  it.each([
    ['control', undefined, 'Enabled'],
    ['readonly', undefined, 'Enabled'],
    ['control', null, '(empty)'],
    ['readonly', null, '(empty)'],
    ['control', '', '(empty)'],
    ['readonly', '', '(empty)'],
    ['control', false, 'Disabled'],
    ['readonly', false, 'Disabled'],
  ])('shows the effective value for a %s row with stored value %j', async (mode, value, expected) => {
    const completion = {
      path: 'dashboard.terminal.completion.enabled', type: 'boolean', label: 'Terminal completion',
      help: '', defaultValue: true,
      allSettings: { mode, page: { tab: 'developer', sub: 'config' }, reason: 'owned' },
    }
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ entries: [completion] }), { status: 200 })))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({
      dashboard: { terminal: { completion: value === undefined ? {} : { enabled: value } } },
    })
    renderWithProviders(<AllSettingsList />)
    const row = await waitFor(() => rowFor(completion.path))
    expect(within(row).getByText(`Current: ${expected}`)).toBeTruthy()
    expect(api.patchConfig).not.toHaveBeenCalled()
  })

  it('links a key with its own control to that page instead of drawing a second control', async () => {
    renderWithProviders(<AllSettingsList />)
    const row = await waitFor(() => rowFor('agent.sandbox'))
    const link = within(row).getByRole('link', { name: /Change it in Developer/ })
    expect(link.getAttribute('href')).toContain('/settings/developer/config')
    // The value shows here too, so reading it does not need the trip.
    expect(within(row).getByText('Current: auto')).toBeTruthy()
    expect(within(row).queryByText('Read only')).toBeNull()
    expect(within(row).queryByRole('textbox')).toBeNull()
    expect(within(row).queryByText('Applies after gateway restart')).toBeNull()
  })

  it('filters by key or label', async () => {
    renderWithProviders(<AllSettingsList />)
    await screen.findByText('Streaming')
    fireEvent.change(screen.getByLabelText('Filter by name, key or description'), { target: { value: 'idle_timeout' } })
    expect(screen.queryByText('Streaming')).toBeNull()
    expect(screen.getByText('Idle timeout')).toBeTruthy()
  })
})

describe('valueAt', () => {
  it('walks a dotted path and stops at a non-object', () => {
    expect(valueAt({ a: { b: 1 } }, 'a.b')).toBe(1)
    expect(valueAt({ a: 3 }, 'a.b')).toBeUndefined()
  })
})
