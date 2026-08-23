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
  jwt_available: true,
  soft_threshold_pct: 80,
  hard_threshold_pct: 95,
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

  it('masks a stored client secret instead of leaving the read-only box empty', async () => {
    // The Teams payload carries no server-rendered preview, so without a
    // stand-in the read-only view of a STORED secret reads as "not set".
    getTeamsConfig.mockResolvedValue({ ...BASE, app_password_set: true, read_only: true })
    renderPanel()
    await heading()
    expect(screen.getByText('••••••')).toBeInTheDocument()
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

describe('TeamsPanel access', () => {
  it('saves the enable toggle and an added principal', async () => {
    renderPanel()
    await heading()
    fireEvent.click(screen.getByLabelText('Enable Teams channel'))
    fireEvent.change(screen.getByPlaceholderText(/you@example.com/), {
      target: { value: ' 00000000-0000-0000-0000-000000000001 ' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^Add$/ }))
    await act(async () => { fireEvent.click(saveBtn()) })
    await waitFor(() => expect(saveTeamsConfig).toHaveBeenCalled())
    expect(saveTeamsConfig.mock.calls[0][0]).toMatchObject({
      enabled: true,
      allowed_emails: ['00000000-0000-0000-0000-000000000001'],
    })
  })

  it('refuses a principal with whitespace in it', async () => {
    renderPanel()
    await heading()
    fireEvent.change(screen.getByPlaceholderText(/you@example.com/), {
      target: { value: 'not a principal' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^Add$/ }))
    expect(screen.getByText(/not a valid ID/i)).toBeInTheDocument()
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

  it('saves a folder name the user typed', async () => {
    renderPanel()
    await heading()
    fireEvent.click(screen.getByLabelText(/File sessions in/i))
    fireEvent.change(screen.getByLabelText('Folder name'), { target: { value: ' zz-typed ' } })
    await act(async () => { fireEvent.click(saveBtn()) })
    await waitFor(() => expect(saveTeamsConfig).toHaveBeenCalled())
    expect(saveTeamsConfig.mock.calls[0][0].session_folder).toBe('zz-typed')
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

describe('TeamsPanel context thresholds', () => {
  it('seeds both fields from the gateway', async () => {
    getTeamsConfig.mockResolvedValue({ ...BASE, soft_threshold_pct: 60, hard_threshold_pct: 85 })
    renderPanel()
    await heading()
    expect((screen.getByLabelText('Soft context threshold %') as HTMLInputElement).value).toBe('60')
    expect((screen.getByLabelText('Hard context threshold %') as HTMLInputElement).value).toBe('85')
  })

  it('leaves a stored percentage alone when the gateway sent none', async () => {
    // A gateway that predates the fields sends neither, and a save that only
    // edits the allow-list must not overwrite what it still holds.
    const { soft_threshold_pct: _s, hard_threshold_pct: _h, ...older } = BASE
    getTeamsConfig.mockResolvedValue(older)
    renderPanel()
    await heading()
    expect((screen.getByLabelText('Soft context threshold %') as HTMLInputElement).value).toBe('')
    await act(async () => { fireEvent.click(saveBtn()) })
    await waitFor(() => expect(saveTeamsConfig).toHaveBeenCalled())
    const payload = saveTeamsConfig.mock.calls[0][0]
    expect('soft_threshold_pct' in payload).toBe(false)
    expect('hard_threshold_pct' in payload).toBe(false)
  })

  it.each([
    ['soft_threshold_pct_invalid', /Soft context threshold must be a number between 1 and 100/i],
    ['hard_threshold_pct_invalid', /Hard context threshold must be a number between 1 and 100/i],
  ])('localizes the %s rejection instead of echoing the wire prose', async (code, expected) => {
    saveTeamsConfig.mockRejectedValue(
      new Error(JSON.stringify({ code, error: `${code} zz-wire-prose` })),
    )
    renderPanel()
    await heading()
    await act(async () => { fireEvent.click(saveBtn()) })
    await waitFor(() => expect(screen.getByText(expected)).toBeInTheDocument())
    expect(screen.queryByText(/zz-wire-prose/)).not.toBeInTheDocument()
  })

  it('refuses a field the user blanked, rather than dropping it', async () => {
    // The other direction from the test above: this gateway DID report the value, so it
    // is on screen, and letting the save drop it means the number the user just cleared
    // comes back under a "Saved." — the field would be lying about what it holds.
    renderPanel()
    await heading()
    fireEvent.change(screen.getByLabelText('Hard context threshold %'), { target: { value: '' } })
    expect(saveBtn()).toBeDisabled()
    await act(async () => { fireEvent.click(saveBtn()) })
    expect(saveTeamsConfig).not.toHaveBeenCalled()
  })

  it('still drops a blank the gateway never reported, and only that one', async () => {
    const { hard_threshold_pct: _h, ...older } = BASE
    getTeamsConfig.mockResolvedValue(older)
    renderPanel()
    await heading()
    await act(async () => { fireEvent.click(saveBtn()) })
    await waitFor(() => expect(saveTeamsConfig).toHaveBeenCalled())
    const payload = saveTeamsConfig.mock.calls[0][0]
    expect(payload.soft_threshold_pct).toBe(80)
    expect('hard_threshold_pct' in payload).toBe(false)
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
