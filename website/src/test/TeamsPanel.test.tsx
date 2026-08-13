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
}

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

  it('is read-only from a remote session (no Save button)', async () => {
    getTeamsConfig.mockResolvedValue({ ...BASE, read_only: true })
    render(ui())
    await screen.findByRole('heading', { name: 'Microsoft Teams' })
    expect(screen.queryByRole('button', { name: /Save Teams settings/ })).not.toBeInTheDocument()
    expect(screen.getAllByText(/read-only from remote sessions/i).length).toBeGreaterThan(0)
  })
})
