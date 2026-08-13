// TeamsPanel — the states the focused suite never reaches: the load/failure
// placeholders, the status pill's three tones, the "why isn't it active" hint,
// the optional session folder, the secret-clear path and the save error branches.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const getTeamsConfig = vi.fn()
const saveTeamsConfig = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    getTeamsConfig: () => getTeamsConfig(),
    saveTeamsConfig: (body: unknown) => saveTeamsConfig(body),
  },
}))

import { TeamsPanel } from '../pages/settings/TeamsPanel'

const BASE = {
  connected: false,
  connect_error: '',
  configured: false,
  read_only: false,
  app_id_set: false,
  app_password_set: false,
  enabled: false,
  tenant_id: '',
  allowed_emails: [] as string[],
  session_folder: '',
}

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <TeamsPanel />
    </QueryClientProvider>,
  )
}

const heading = () => screen.findByRole('heading', { name: 'Microsoft Teams' })
const saveBtn = () => screen.getByRole('button', { name: /Save Teams settings/ })

beforeEach(() => {
  getTeamsConfig.mockReset().mockResolvedValue({ ...BASE })
  saveTeamsConfig.mockReset().mockResolvedValue({ ok: true, restart_required: false })
})

afterEach(() => { vi.useRealTimers() })

describe('TeamsPanel load states', () => {
  it('shows a placeholder while the config is in flight', () => {
    getTeamsConfig.mockReturnValue(new Promise(() => {}))
    renderPanel()
    expect(screen.getByText(/Loading Teams config/)).toBeInTheDocument()
  })

  it('explains a load failure instead of rendering an empty form', async () => {
    getTeamsConfig.mockRejectedValue(new Error('down'))
    renderPanel()
    expect(await screen.findByText(/Cannot load Teams config/)).toBeInTheDocument()
  })
})

describe('TeamsPanel status', () => {
  it('reads Active when the channel is connected', async () => {
    getTeamsConfig.mockResolvedValue({ ...BASE, connected: true, configured: true })
    renderPanel()
    await heading()
    expect(screen.getByText('Active')).toBeInTheDocument()
  })

  it('reads Not active with a restart hint when configured but down', async () => {
    getTeamsConfig.mockResolvedValue({ ...BASE, configured: true })
    renderPanel()
    await heading()
    expect(screen.getByText('Not active')).toBeInTheDocument()
    expect(screen.getByText(/not running/i)).toBeInTheDocument()
  })

  it('surfaces the credential error when one is reported', async () => {
    getTeamsConfig.mockResolvedValue({ ...BASE, configured: true, connect_error: 'zz-bad-secret' })
    renderPanel()
    await heading()
    expect(screen.getByText(/zz-bad-secret/)).toBeInTheDocument()
  })

  it('reads Needs setup and shows no hint before anything is configured', async () => {
    renderPanel()
    await heading()
    expect(screen.getByText('Needs setup')).toBeInTheDocument()
    expect(screen.queryByText(/not running/i)).not.toBeInTheDocument()
  })

  it('masks a stored App ID rather than pre-filling it', async () => {
    getTeamsConfig.mockResolvedValue({ ...BASE, app_id_set: true, configured: true })
    renderPanel()
    await heading()
    const field = screen.getByLabelText('App (Client) ID') as HTMLInputElement
    expect(field.value).toBe('')
    expect(field.placeholder).toContain('paste to replace')
  })
})

describe('TeamsPanel session folder', () => {
  it('hides the folder name until filing is turned on', async () => {
    renderPanel()
    await heading()
    expect(screen.queryByLabelText('Folder name')).not.toBeInTheDocument()
    fireEvent.click(screen.getByLabelText(/File sessions in/i))
    expect(screen.getByLabelText('Folder name')).toBeInTheDocument()
  })

  it('derives the on-state from a persisted folder name', async () => {
    getTeamsConfig.mockResolvedValue({ ...BASE, session_folder: 'zz-folder' })
    renderPanel()
    await heading()
    expect((screen.getByLabelText('Folder name') as HTMLInputElement).value).toBe('zz-folder')
  })

  it('falls back to the channel name when filing is on but blank', async () => {
    renderPanel()
    await heading()
    fireEvent.click(screen.getByLabelText(/File sessions in/i))
    await act(async () => { fireEvent.click(saveBtn()) })
    await waitFor(() => expect(saveTeamsConfig).toHaveBeenCalled())
    expect(saveTeamsConfig.mock.calls[0][0].session_folder).toBe('Teams')
  })

  it('sends the off-state as an empty folder', async () => {
    getTeamsConfig.mockResolvedValue({ ...BASE, session_folder: 'zz-folder' })
    renderPanel()
    await heading()
    fireEvent.click(screen.getByLabelText(/File sessions in/i))
    await act(async () => { fireEvent.click(saveBtn()) })
    await waitFor(() => expect(saveTeamsConfig).toHaveBeenCalled())
    expect(saveTeamsConfig.mock.calls[0][0].session_folder).toBe('')
  })
})

describe('TeamsPanel save', () => {
  it('sends a typed secret and confirms the save', async () => {
    renderPanel()
    await heading()
    fireEvent.change(screen.getByPlaceholderText(/client secret/i), { target: { value: ' zz-secret ' } })
    fireEvent.change(screen.getByLabelText('Tenant ID'), { target: { value: ' zz-tenant ' } })
    await act(async () => { fireEvent.click(saveBtn()) })
    await waitFor(() => expect(screen.getByText('Saved.')).toBeInTheDocument())
    expect(saveTeamsConfig.mock.calls[0][0]).toMatchObject({
      app_password: 'zz-secret',
      tenant_id: 'zz-tenant',
    })
  })

  it('asks for a restart when the backend says one is required', async () => {
    saveTeamsConfig.mockResolvedValue({ ok: true, restart_required: true })
    renderPanel()
    await heading()
    await act(async () => { fireEvent.click(saveBtn()) })
    await waitFor(() => expect(screen.getByText(/Restart the gateway to apply/)).toBeInTheDocument())
  })

  it('unwraps a JSON error body from the failed save', async () => {
    saveTeamsConfig.mockRejectedValue(new Error(JSON.stringify({ error: 'zz-json-reason' })))
    renderPanel()
    await heading()
    await act(async () => { fireEvent.click(saveBtn()) })
    await waitFor(() => expect(screen.getByText('zz-json-reason')).toBeInTheDocument())
  })

  it('falls back to the raw message when the body is not JSON', async () => {
    saveTeamsConfig.mockRejectedValue(new Error('zz-plain-reason'))
    renderPanel()
    await heading()
    await act(async () => { fireEvent.click(saveBtn()) })
    await waitFor(() => expect(screen.getByText('zz-plain-reason')).toBeInTheDocument())
  })

  it('falls back to a generic message when the rejection carries none', async () => {
    saveTeamsConfig.mockRejectedValue({ nope: true })
    renderPanel()
    await heading()
    await act(async () => { fireEvent.click(saveBtn()) })
    await waitFor(() => expect(screen.getByText(/Save failed/)).toBeInTheDocument())
  })

  it('clears the transient save confirmation on its timer', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    renderPanel()
    await heading()
    await act(async () => { fireEvent.click(saveBtn()) })
    await waitFor(() => expect(screen.getByText('Saved.')).toBeInTheDocument())
    await act(async () => { vi.advanceTimersByTime(6500) })
    expect(screen.queryByText('Saved.')).not.toBeInTheDocument()
  })

  it('clears the transient error on its timer', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    saveTeamsConfig.mockRejectedValue(new Error('zz-plain-reason'))
    renderPanel()
    await heading()
    await act(async () => { fireEvent.click(saveBtn()) })
    await waitFor(() => expect(screen.getByText('zz-plain-reason')).toBeInTheDocument())
    await act(async () => { vi.advanceTimersByTime(8500) })
    expect(screen.queryByText('zz-plain-reason')).not.toBeInTheDocument()
  })
})
