import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

vi.mock('../../api/client', () => ({
  api: {
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { AllSettingsList, valueAt } from './AllSettingsList'

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
}

function rowFor(key: string): HTMLElement {
  const row = document.querySelector<HTMLElement>(`[data-setting-key="${key}"]`)
  if (!row) throw new Error(`no row for ${key}`)
  return row
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
    // A restart key names what restarts once, in its badge; the caption stays "Saves at once".
    expect(within(rowFor('mcp_gateway.idle_timeout_secs')).getByText('Applies after gateway restart')).toBeTruthy()
    expect(within(rowFor('mcp_gateway.idle_timeout_secs')).getByText('Saves at once')).toBeTruthy()
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
    expect(within(rowFor('mcp_gateway.keep_alive_servers')).getByText('Saves at once')).toBeTruthy()
  })

  it('saves a number as a number and resets a changed key with unset', async () => {
    renderWithProviders(<AllSettingsList />)
    const field = await screen.findByLabelText('Idle timeout')
    fireEvent.change(field, { target: { value: '120' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('mcp_gateway.idle_timeout_secs', 120, undefined))

    fireEvent.click(within(rowFor('mcp_gateway.keep_alive_servers')).getByRole('button', { name: /Reset mcp_gateway.keep_alive_servers/ }))
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('mcp_gateway.keep_alive_servers', undefined, { unset: true }))
    // A key at its default still shows its reset, disabled, so the undo is visible before any change.
    const atDefault = within(rowFor('agent.streaming')).getByRole('button', { name: /Reset agent.streaming/ })
    expect((atDefault as HTMLButtonElement).disabled).toBe(true)
    expect(within(rowFor('agent.streaming')).getByText('Saves at once')).toBeTruthy()
    // A read-only row labels its value and drops the restart badge.
    expect(within(rowFor('agent.dangerously_skip_permissions')).getByText('Current: Disabled')).toBeTruthy()
    expect(within(rowFor('agent.dangerously_skip_permissions')).queryByText('Applies after gateway restart')).toBeNull()
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
    // "Saves at once" would contradict the refusal.
    expect(within(row).queryByText('Saves at once')).toBeNull()
    fireEvent.click(within(row).getByRole('button', { name: 'Undo my edit (300)' }))
    await waitFor(() => expect((screen.getByLabelText('Idle timeout') as HTMLInputElement).value).toBe('300'))
    expect(within(row).queryByText('Not saved')).toBeNull()
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

  it('offers to put back the value a save or a reset replaced', async () => {
    const kc = api.kirocrewConfig as ReturnType<typeof vi.fn>
    renderWithProviders(<AllSettingsList />)
    const box = await screen.findByLabelText('Keep-alive servers')
    const row = rowFor('mcp_gateway.keep_alive_servers')
    // No undo before any save.
    expect(within(row).queryByRole('button', { name: /Undo my edit/ })).toBeNull()

    // The gateway names what the file held before each write.
    ;(api.patchConfig as ReturnType<typeof vi.fn>).mockResolvedValue({ ...CONFIG, previous: { stored: true, value: ['github'] } })
    kc.mockResolvedValue({ ...CONFIG, mcp_gateway: { ...CONFIG.mcp_gateway, keep_alive_servers: ['github', 'slack'] } })
    fireEvent.change(box, { target: { value: 'github\nslack' } })
    fireEvent.blur(box)
    const undoSave = await within(row).findByRole('button', { name: 'Undo my edit (github)' })
    kc.mockResolvedValue(CONFIG)
    fireEvent.click(undoSave)
    await waitFor(() => expect(api.patchConfig).toHaveBeenLastCalledWith('mcp_gateway.keep_alive_servers', ['github'], undefined))
    // Once the undo has saved, it is gone.
    await waitFor(() => expect(within(row).queryByRole('button', { name: /Undo my edit/ })).toBeNull())

    // A reset can be taken back too; the undo restores the custom list.
    kc.mockResolvedValue({ ...CONFIG, mcp_gateway: { ...CONFIG.mcp_gateway, keep_alive_servers: [] } })
    fireEvent.click(within(row).getByRole('button', { name: /Reset mcp_gateway.keep_alive_servers/ }))
    fireEvent.click(await within(row).findByRole('button', { name: 'Undo my edit (github)' }))
    await waitFor(() => expect(api.patchConfig).toHaveBeenLastCalledWith('mcp_gateway.keep_alive_servers', ['github'], undefined))
  })

  it('puts back an absent key as absent, not as a copy of its default', async () => {
    // The config view fills the default in (streaming: true) although config.json has no
    // such leaf; only the PATCH reply's `previous` says it was not stored.
    const kc = api.kirocrewConfig as ReturnType<typeof vi.fn>
    ;(api.patchConfig as ReturnType<typeof vi.fn>).mockResolvedValue({ ...CONFIG, previous: { stored: false } })
    renderWithProviders(<AllSettingsList />)
    const toggle = await within(await waitFor(() => rowFor('agent.streaming'))).findByRole('switch')
    kc.mockResolvedValue({ ...CONFIG, agent: { streaming: false, dangerously_skip_permissions: false } })
    fireEvent.click(toggle)
    fireEvent.click(await within(rowFor('agent.streaming')).findByRole('button', { name: /Undo my edit/ }))
    await waitFor(() => expect(api.patchConfig).toHaveBeenLastCalledWith('agent.streaming', undefined, { unset: true }))
  })

  it('links a key with its own control to that page instead of drawing a second control', async () => {
    renderWithProviders(<AllSettingsList />)
    const row = await waitFor(() => rowFor('agent.sandbox'))
    const link = within(row).getByRole('link', { name: /Change it in Developer/ })
    expect(link.getAttribute('href')).toContain('/settings/developer/config')
    // The value shows here too, so reading it does not need the trip.
    expect(within(row).getByText('Current: (empty)')).toBeTruthy()
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
