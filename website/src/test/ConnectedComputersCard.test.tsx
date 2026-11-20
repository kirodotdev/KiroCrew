import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import { __resetAuthRecoveryStateForTests } from '../api/client'
import { ConnectedComputersCard } from '../pages/settings/ConnectedComputersCard'
import '../i18n'

// Issue 29949: the remote Session Environments device list.
// The backend is authoritative: the card only renders what
// GET /api/connected-computers/devices returns, and a 403 (flag off) hides it.

const row = (over: Record<string, unknown>) => ({
  device_id: 'laptop', label: 'laptop', status: 'active', created: 1, updated: 1,
  connected: false, root: '', confinement: '', label_is_shared: false, ...over,
})

function renderCard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><ConnectedComputersCard /></QueryClientProvider>)
}

// The API layer keeps an `owner_only` GET refusal per URL for a minute
// (client.ts `refusedReads`), so the auth-refusal case would answer every later
// case's device read from that copy. Forget it between cases.
afterEach(() => {
  cleanup()
  __resetAuthRecoveryStateForTests()
})

describe('ConnectedComputersCard', () => {
  it('flag off: a backend refusal renders nothing (no placeholder control)', async () => {
    server.use(http.get('/api/connected-computers/devices', () =>
      HttpResponse.json({ code: 'connected_computers_disabled', error: 'off' }, { status: 403 })))
    const { container } = renderCard()
    expect(screen.getByText('Connected computers')).toBeTruthy()
    await waitFor(() => expect(screen.queryByText('Connected computers')).toBeNull())
    expect(container.querySelector('[data-cc-state]')).toBeNull()
  })

  it('B09/B10: same labels show the stable id; online, offline and revoked states are explicit', async () => {
    server.use(http.get('/api/connected-computers/devices', () => HttpResponse.json({ devices: [
      row({ device_id: 'laptop', connected: true, root: '/home/u/proj', label_is_shared: true }),
      row({ device_id: 'laptop-2', label_is_shared: true }),
      row({ device_id: 'old', label: 'old', status: 'revoked' }),
    ] })))
    renderCard()
    expect(await screen.findByText('ID laptop-2')).toBeTruthy()
    expect(screen.getByText('Connected computers')).toBeTruthy()
    expect(document.querySelector('[data-cc-device="laptop"]')?.getAttribute('data-cc-state')).toBe('online')
    expect(document.querySelector('[data-cc-device="laptop-2"]')?.getAttribute('data-cc-state')).toBe('offline')
    expect(document.querySelector('[data-cc-device="old"]')?.getAttribute('data-cc-state')).toBe('revoked')
  })

  it('B12: removed computers are not listed and Remove never deletes files (it only calls the remove route)', async () => {
    const calls: string[] = []
    server.use(
      http.get('/api/connected-computers/devices', () => HttpResponse.json({ devices: [
        row({ device_id: 'laptop' }), row({ device_id: 'gone', label: 'gone', status: 'removed' }),
      ] })),
      http.post('/api/connected-computers/devices/:id/remove', ({ params }) => {
        calls.push(String(params.id))
        return HttpResponse.json({ device: row({ device_id: String(params.id), status: 'removed' }) })
      }),
    )
    renderCard()
    const remove = await screen.findByRole('button', { name: 'Remove laptop' })
    expect(document.querySelector('[data-cc-device="gone"]')).toBeNull()
    fireEvent.click(remove)
    // Remove is not undone by sharing again (a removed computer never re-enrolls): it asks first.
    const dialog = await screen.findByRole('dialog')
    expect(calls).toEqual([])
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(calls).toEqual(['laptop']))
  })

  it('B12: cancelling the Remove question removes nothing', async () => {
    const calls: string[] = []
    server.use(
      http.get('/api/connected-computers/devices', () => HttpResponse.json({ devices: [row({ device_id: 'laptop' })] })),
      http.post('/api/connected-computers/devices/:id/remove', ({ params }) => {
        calls.push(String(params.id))
        return HttpResponse.json({ device: row({ device_id: String(params.id), status: 'removed' }) })
      }),
    )
    renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Remove laptop' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(calls).toEqual([])
  })

  it('an auth refusal is shown, not hidden like the feature being off', async () => {
    server.use(http.get('/api/connected-computers/devices', () =>
      HttpResponse.json({ code: 'owner_only', error: 'Only the dashboard owner can do that' }, { status: 403 })))
    renderCard()
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText('Connected computers')).toBeTruthy()
  })

  it('a failed revoke names the computer and keeps the row', async () => {
    server.use(
      http.get('/api/connected-computers/devices', () => HttpResponse.json({ devices: [row({ device_id: 'a', label: 'a' })] })),
      http.post('/api/connected-computers/devices/:id/revoke', () =>
        HttpResponse.json({ code: 'internal', error: 'vault locked' }, { status: 500 })),
    )
    renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect a' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Could not disconnect a/)
    expect(document.querySelector('[data-cc-device="a"]')).not.toBeNull()
  })

  it('busy: actions are disabled while a revoke is in flight', async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>(resolve => { release = resolve })
    server.use(
      http.get('/api/connected-computers/devices', () => HttpResponse.json({ devices: [
        row({ device_id: 'a', label: 'a' }), row({ device_id: 'b', label: 'b' }),
      ] })),
      http.post('/api/connected-computers/devices/:id/revoke', async ({ params }) => {
        await gate
        return HttpResponse.json({ device: row({ device_id: String(params.id), status: 'revoked' }) })
      }),
    )
    renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect a' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Disconnect b' })).toBeDisabled())
    release()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Remove b' })).toBeEnabled())
  })

  it('B07/B11: Disconnect (revoke) calls only that device', async () => {
    const calls: string[] = []
    server.use(
      http.get('/api/connected-computers/devices', () => HttpResponse.json({ devices: [
        row({ device_id: 'a', label: 'a' }), row({ device_id: 'b', label: 'b' }),
      ] })),
      http.post('/api/connected-computers/devices/:id/revoke', ({ params }) => {
        calls.push(String(params.id))
        return HttpResponse.json({ device: row({ device_id: String(params.id), status: 'revoked' }) })
      }),
    )
    renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect a' }))
    await waitFor(() => expect(calls).toEqual(['a']))
  })
})
