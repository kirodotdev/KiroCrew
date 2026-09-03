/**
 * TeamsPanel — Microsoft Teams channel settings. Verifies the panel loads its
 * config, renders the credential fields, and that Save posts the draft (with
 * the secret write-only) to PUT /api/teams/config.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
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

function ui() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <TeamsPanel />
    </QueryClientProvider>
  )
}

const BASE = {
  connected: false,
  connect_error: '',
  configured: false,
  read_only: false,
  app_id_set: false,
  app_password_set: false,
  enabled: false,
  tenant_id: '',
  allowed_emails: ['alice@example.com'],
  jwt_available: true,
  soft_threshold_pct: 80,
  hard_threshold_pct: 95,
}

const SOFT_LABEL = 'Soft context threshold %'
const HARD_LABEL = 'Hard context threshold %'

beforeEach(() => {
  getTeamsConfig.mockReset().mockResolvedValue({ ...BASE })
  saveTeamsConfig.mockReset().mockResolvedValue({ ok: true, restart_required: true, verify_warning: '' })
})

describe('TeamsPanel', () => {
  it('renders header + credential fields once config loads', async () => {
    render(ui())
    expect(await screen.findByRole('heading', { name: 'Microsoft Teams' })).toBeInTheDocument()
    // target the form field by its label (the steps section also mentions the name)
    expect(screen.getByLabelText('App (Client) ID')).toBeInTheDocument()
    expect(screen.getByText('App password (client secret)')).toBeInTheDocument()
    // webhook endpoint hint is surfaced for Azure setup
    expect((await screen.findAllByText(/\/api\/messaging\/teams/)).length).toBeGreaterThan(0)
  })

  it('Save posts the draft (enabled/app_id/tenant/allowed) to saveTeamsConfig', async () => {
    render(ui())
    await screen.findByRole('heading', { name: 'Microsoft Teams' })
    fireEvent.change(screen.getByPlaceholderText('Microsoft App ID'), { target: { value: 'app-xyz' } })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Save Teams settings/ }))
    })
    await waitFor(() => expect(saveTeamsConfig).toHaveBeenCalledTimes(1))
    const payload = saveTeamsConfig.mock.calls[0][0]
    expect(payload.app_id).toBe('app-xyz')
    expect(payload.enabled).toBe(false)
    expect(payload.allowed_emails).toEqual(['alice@example.com'])
    // secret is write-only: not sent unless typed
    expect('app_password' in payload).toBe(false)
  })

  it('Save omits app_id when already set and not re-entered (no wipe)', async () => {
    // App ID stored → field renders masked and draft.app_id loads blank. A save
    // that only edits other fields must NOT send app_id: "" (which would wipe
    // the stored value and disable the channel at next boot).
    getTeamsConfig.mockResolvedValue({ ...BASE, app_id_set: true, configured: true })
    render(ui())
    await screen.findByRole('heading', { name: 'Microsoft Teams' })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Save Teams settings/ }))
    })
    await waitFor(() => expect(saveTeamsConfig).toHaveBeenCalledTimes(1))
    const payload = saveTeamsConfig.mock.calls[0][0]
    expect('app_id' in payload).toBe(false)
  })

  it('is read-only from a remote session (no Save button, every field disabled)', async () => {
    getTeamsConfig.mockResolvedValue({ ...BASE, read_only: true, tenant_id: 'zz-tenant' })
    render(ui())
    await screen.findByRole('heading', { name: 'Microsoft Teams' })
    expect(screen.queryByRole('button', { name: /Save Teams settings/ })).not.toBeInTheDocument()
    expect(screen.getAllByText(/read-only from remote sessions/i).length).toBeGreaterThan(0)
    // The values are still READABLE — a remote operator has to be able to see
    // what the machine running the gateway is configured with.
    expect((screen.getByLabelText('Tenant ID') as HTMLInputElement).value).toBe('zz-tenant')
    for (const label of ['App (Client) ID', 'Tenant ID', SOFT_LABEL, HARD_LABEL]) {
      expect(screen.getByLabelText(label)).toBeDisabled()
    }
  })

  it('sends both context thresholds as integers', async () => {
    render(ui())
    await screen.findByRole('heading', { name: 'Microsoft Teams' })
    fireEvent.change(screen.getByLabelText(SOFT_LABEL), { target: { value: '70' } })
    fireEvent.change(screen.getByLabelText(HARD_LABEL), { target: { value: '90' } })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Save Teams settings/ }))
    })
    await waitFor(() => expect(saveTeamsConfig).toHaveBeenCalledTimes(1))
    expect(saveTeamsConfig.mock.calls[0][0]).toMatchObject({
      soft_threshold_pct: 70,
      hard_threshold_pct: 90,
    })
  })
})

describe('TeamsPanel — the PyJWT dependency', () => {
  it('explains the missing extra, with the command that installs it', async () => {
    getTeamsConfig.mockResolvedValue({ ...BASE, jwt_available: false, configured: true })
    render(ui())
    await screen.findByRole('heading', { name: 'Microsoft Teams' })
    const notice = screen.getByRole('alert')
    expect(notice).toHaveTextContent('PyJWT')
    expect(notice).toHaveTextContent('pip install "PyJWT[crypto]==2.13.0"')
    // The command must stay copy-pasteable, so it renders as a code token the
    // pseudolocale scanner and the reader both treat as literal.
    expect(notice.querySelector('code')?.textContent).toBe('pip install "PyJWT[crypto]==2.13.0"')
  })

  it('does not blame a restart while the dependency is what is missing', async () => {
    getTeamsConfig.mockResolvedValue({ ...BASE, jwt_available: false, configured: true })
    render(ui())
    await screen.findByRole('heading', { name: 'Microsoft Teams' })
    expect(screen.queryByText(/not running/i)).not.toBeInTheDocument()
  })

  it('stays quiet when PyJWT is installed', async () => {
    render(ui())
    await screen.findByRole('heading', { name: 'Microsoft Teams' })
    expect(screen.queryByText(/PyJWT/)).not.toBeInTheDocument()
  })

  it('stays quiet when the gateway reports no answer either way', async () => {
    // A gateway that predates the field sends nothing, and absence must not be
    // read as a missing dependency.
    const { jwt_available: _omitted, ...withoutField } = BASE
    getTeamsConfig.mockResolvedValue(withoutField)
    render(ui())
    await screen.findByRole('heading', { name: 'Microsoft Teams' })
    expect(screen.queryByText(/PyJWT/)).not.toBeInTheDocument()
  })
})

describe('TeamsPanel — threshold validation', () => {
  it('rejects a hard threshold below the soft one before any request', async () => {
    render(ui())
    await screen.findByRole('heading', { name: 'Microsoft Teams' })
    fireEvent.change(screen.getByLabelText(HARD_LABEL), { target: { value: '50' } })
    expect(screen.getByText(/at or above the soft context threshold/i)).toBeInTheDocument()
    const save = screen.getByRole('button', { name: /Save Teams settings/ })
    expect(save).toBeDisabled()
    await act(async () => { fireEvent.click(save) })
    expect(saveTeamsConfig).not.toHaveBeenCalled()
  })

  it('accepts an equal pair — hard >= soft, not hard > soft', async () => {
    render(ui())
    await screen.findByRole('heading', { name: 'Microsoft Teams' })
    fireEvent.change(screen.getByLabelText(HARD_LABEL), { target: { value: '80' } })
    expect(screen.queryByText(/at or above the soft context threshold/i)).not.toBeInTheDocument()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Save Teams settings/ }))
    })
    await waitFor(() => expect(saveTeamsConfig).toHaveBeenCalledTimes(1))
    expect(saveTeamsConfig.mock.calls[0][0].hard_threshold_pct).toBe(80)
  })

  it('refuses a cleared field instead of silently keeping the stored value', async () => {
    // Clearing the box is how a user tries to stop the nudge. Treating blank as "keep
    // what is stored" makes that attempt end in "Saved." with the old number back, and
    // a placeholder cannot carry the difference -- it reads as an example. So blank is
    // an error, said at the moment it happens, and Save is unavailable until it is fixed.
    render(ui())
    await screen.findByRole('heading', { name: 'Microsoft Teams' })
    fireEvent.change(screen.getByLabelText(SOFT_LABEL), { target: { value: '' } })

    expect(screen.getByText(/Soft context threshold must be a number between 1 and 100/i))
      .toBeInTheDocument()
    const save = screen.getByRole('button', { name: /Save Teams settings/ })
    expect(save).toBeDisabled()
    await act(async () => { fireEvent.click(save) })
    expect(saveTeamsConfig).not.toHaveBeenCalled()
    // The stored value is still shown, so the box says what it is refusing to lose.
    expect(screen.getByLabelText(SOFT_LABEL)).toHaveAttribute('placeholder', '80')
  })

  it('names the field that is out of range, per field', async () => {
    render(ui())
    await screen.findByRole('heading', { name: 'Microsoft Teams' })
    fireEvent.change(screen.getByLabelText(SOFT_LABEL), { target: { value: '0' } })
    expect(screen.getByText(/Soft context threshold must be a number between 1 and 100/i))
      .toBeInTheDocument()
    fireEvent.change(screen.getByLabelText(SOFT_LABEL), { target: { value: '80' } })
    fireEvent.change(screen.getByLabelText(HARD_LABEL), { target: { value: '101' } })
    expect(screen.getByText(/Hard context threshold must be a number between 1 and 100/i))
      .toBeInTheDocument()
  })

  it('says a known rejection code in the user\'s language, not the wire prose', async () => {
    // The backend sends `code` alongside advisory English prose precisely so the
    // panel can localize it; rendering the prose would put English in a
    // translated panel.
    saveTeamsConfig.mockRejectedValue(new Error(JSON.stringify({
      code: 'threshold_pct_inverted',
      error: 'hard_threshold_pct must be greater than or equal to soft_threshold_pct',
    })))
    render(ui())
    await screen.findByRole('heading', { name: 'Microsoft Teams' })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Save Teams settings/ }))
    })
    await waitFor(() =>
      expect(screen.getByText(/at or above the soft context threshold/i)).toBeInTheDocument())
    expect(screen.queryByText(/hard_threshold_pct must be/)).not.toBeInTheDocument()
  })

  it('still renders a 400 the backend answers with, when only a code comes back', async () => {
    // The client-side check mirrors the backend's rule, so this path is reached
    // only when the two disagree — it must still say something readable.
    saveTeamsConfig.mockRejectedValue(new Error(JSON.stringify({ code: 'zz_threshold_invalid' })))
    render(ui())
    await screen.findByRole('heading', { name: 'Microsoft Teams' })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Save Teams settings/ }))
    })
    await waitFor(() =>
      expect(screen.getByText(/rejected these settings \(zz_threshold_invalid\)/)).toBeInTheDocument())
  })
})
