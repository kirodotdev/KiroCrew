import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../api/client'
import type { InstanceView } from '../api/client/instances'
import type { LocalGrant } from '../api/client/connectedComputers'
import { ApiError } from '../api/apiError'
import { LocalSharingCard, grantFailure } from '../pages/settings/LocalSharingCard'
import '../i18n'

// Issue 29949: this computer's own sharing controls (Remote Crew settings).
// Sharing needs native consent in the desktop shell; the page only names the
// crew. Narrowing (stop one grant, Off) works without the bridge or the switch.

const crew = (over: Partial<InstanceView>): InstanceView => ({
  id: 'build', name: 'Build box', ssh_host: 'build.example', remote_port: 1, local_port: 2, ttl: '',
  remote_bin: '', connection_method: 'ssh', ssm_target: '', aws_profile: '', aws_region: '', ssm_run_as: '',
  ...over,
} as InstanceView)

const grant = (over: Partial<LocalGrant>): LocalGrant => ({
  grant_id: 'g1', instance_id: 'build', gateway_id: 'gw', gateway_label: 'Build box', device_id: 'laptop',
  root: '/home/u/proj', mcp: [], status: 'active', created: 1, revoked_at: null, state: 'connected', ...over,
})

function mockInstances(instances: InstanceView[]) {
  return vi.spyOn(api, 'listInstances').mockResolvedValue({
    active: true, instances, warm_set_cap: 0, sso: {} as never,
  } as Awaited<ReturnType<typeof api.listInstances>>)
}
function mockGrants(grants: LocalGrant[], enabled = true) {
  return vi.spyOn(api, 'connectedComputersLocalGrants').mockResolvedValue({ grants, enabled })
}
function setBridge(confirm?: (consent: string) => Promise<unknown>) {
  const w = window as { connectedComputersAPI?: unknown }
  if (confirm) w.connectedComputersAPI = { confirm }
  else delete w.connectedComputersAPI
}
function renderCard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return { ...render(<QueryClientProvider client={qc}><LocalSharingCard /></QueryClientProvider>), queryClient: qc }
}
async function renderCardWithInstancesReady() {
  const { queryClient } = renderCard()
  await act(async () => {
    await waitFor(() => expect(queryClient.getQueryState(['instances'])?.status).toBe('success'))
  })
}
const CONSENT = 'c'.repeat(32)

beforeEach(() => { vi.restoreAllMocks() })
afterEach(() => { cleanup(); setBridge() })

describe('LocalSharingCard', () => {
  it('B13: with no desktop bridge it cannot share, and says why', async () => {
    mockInstances([crew({})])
    mockGrants([])
    const consent = vi.spyOn(api, 'connectedComputersLocalConsent')
    await renderCardWithInstancesReady()
    expect(await screen.findByText(/needs the Kiro Crew desktop app/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Share a folder with/ })).toBeNull()
    expect(consent).not.toHaveBeenCalled()
  })

  it('B02/B13: Share sends only the crew id; the native dialog gets only the consent id', async () => {
    mockInstances([crew({})])
    mockGrants([])
    const consent = vi.spyOn(api, 'connectedComputersLocalConsent').mockResolvedValue({
      consent: CONSENT, expires_in: 120, instance: { id: 'build', name: 'Build box' }, mcp: [],
    })
    const confirm = vi.fn(async () => ({ ok: true, grant: {} }))
    setBridge(confirm)
    renderCard()
    await userEvent.click(await screen.findByRole('button', { name: 'Share a folder with Build box' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledWith(CONSENT))
    expect(consent).toHaveBeenCalledWith('build')
    expect(consent.mock.calls[0]).toHaveLength(1)
  })

  it('B02: a cancelled native dialog shares nothing and shows no error', async () => {
    mockInstances([crew({})])
    const grants = mockGrants([])
    vi.spyOn(api, 'connectedComputersLocalConsent').mockResolvedValue({
      consent: CONSENT, expires_in: 120, instance: { id: 'build', name: 'Build box' }, mcp: [],
    })
    setBridge(async () => ({ ok: false, error: 'cancelled' }))
    renderCard()
    await userEvent.click(await screen.findByRole('button', { name: 'Share a folder with Build box' }))
    expect(await screen.findByText('Nothing was shared.')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(grants).toHaveBeenCalled()
  })

  it('busy: every Share button is disabled while the native dialog is open', async () => {
    mockInstances([crew({}), crew({ id: 'ci', name: 'CI box' })])
    mockGrants([])
    vi.spyOn(api, 'connectedComputersLocalConsent').mockResolvedValue({
      consent: CONSENT, expires_in: 120, instance: { id: 'build', name: 'Build box' }, mcp: [],
    })
    let answer: (v: unknown) => void = () => {}
    const confirm = vi.fn(() => new Promise(resolve => { answer = resolve }))
    setBridge(confirm)
    renderCard()
    await userEvent.click(await screen.findByRole('button', { name: 'Share a folder with Build box' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledWith(CONSENT))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Share a folder with CI box' })).toBeDisabled())
    expect(screen.getByText(/Answer in the desktop app/)).toBeTruthy()
    answer({ ok: false, error: 'cancelled' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Share a folder with CI box' })).toBeEnabled())
  })

  it.each([
    ['protection_unavailable', /no system keychain or keyring/],
    ['turned_off', /turned off while you were choosing/],
    ['enrollment_failed', /could not be reached over SSH/],
  ])('a refused share (%s) is shown in words', async (code, text) => {
    mockInstances([crew({})])
    mockGrants([])
    vi.spyOn(api, 'connectedComputersLocalConsent').mockResolvedValue({
      consent: CONSENT, expires_in: 120, instance: { id: 'build', name: 'Build box' }, mcp: [],
    })
    setBridge(async () => ({ ok: false, error: code }))
    renderCard()
    await userEvent.click(await screen.findByRole('button', { name: 'Share a folder with Build box' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(text)
  })

  it('feature off here: no Share, but existing grants can still be stopped', async () => {
    mockInstances([crew({})])
    mockGrants([grant({})], false)
    setBridge(async () => ({ ok: true }))
    await renderCardWithInstancesReady()
    expect(await screen.findByText(/Connected computers is turned off on this computer/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Share a folder with/ })).toBeNull()
    expect(screen.getByRole('button', { name: 'Stop sharing with Build box' })).toBeEnabled()
  })

  it('an auth failure reading grants is shown, not hidden as "feature off"', async () => {
    mockInstances([crew({})])
    vi.spyOn(api, 'connectedComputersLocalGrants').mockRejectedValue(
      new ApiError(403, 'Only the dashboard owner can do that', '{"code":"owner_only"}'))
    renderCard()
    expect(await screen.findByRole('alert')).toHaveTextContent('Only the dashboard owner can do that')
  })

  it('B07: Stop sharing ends only that grant', async () => {
    mockInstances([crew({}), crew({ id: 'ci', name: 'CI box' })])
    mockGrants([grant({}), grant({ grant_id: 'g2', instance_id: 'ci', gateway_label: 'CI box' })])
    const revoke = vi.spyOn(api, 'connectedComputersLocalRevoke').mockResolvedValue({ revoked: 'g1' })
    renderCard()
    await userEvent.click(await screen.findByRole('button', { name: 'Stop sharing with Build box' }))
    await waitFor(() => expect(revoke).toHaveBeenCalledWith('g1'))
    expect(revoke).toHaveBeenCalledTimes(1)
  })

  it('Off asks first; Cancel turns nothing off', async () => {
    mockInstances([crew({})])
    mockGrants([grant({})])
    const off = vi.spyOn(api, 'connectedComputersLocalOff').mockResolvedValue({ revoked: ['g1'] })
    renderCard()
    await userEvent.click(await screen.findByRole('button', { name: 'Turn off all sharing' }))
    const dialog = await screen.findByRole('dialog')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(off).not.toHaveBeenCalled()
  })

  it('Off, confirmed, turns every grant off', async () => {
    mockInstances([crew({})])
    mockGrants([grant({})])
    const off = vi.spyOn(api, 'connectedComputersLocalOff').mockResolvedValue({ revoked: ['g1'] })
    renderCard()
    await userEvent.click(await screen.findByRole('button', { name: 'Turn off all sharing' }))
    const dialog = await screen.findByRole('dialog')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Turn off' }))
    await waitFor(() => expect(off).toHaveBeenCalledTimes(1))
  })

  it('only Remote Crews reached over SSH are offered', async () => {
    mockInstances([crew({}), crew({ id: 'ssm', name: 'SSM box', connection_method: 'ssm', ssh_host: '' })])
    mockGrants([])
    setBridge(async () => ({ ok: true }))
    renderCard()
    expect(await screen.findByRole('button', { name: 'Share a folder with Build box' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Share a folder with SSM box' })).toBeNull()
  })
})

describe('grant failure details', () => {
  it.each([
    ['unavailable: confinement self-test failed', 'Unavailable', 'confinement self-test failed'],
    ['refused: the system keyring changed', 'Refused', 'the system keyring changed'],
    ['disconnected: protocol error', 'Disconnected', 'protocol error'],
    ['refused by the remote gateway; share again', 'Refused', 'refused by the remote gateway; share again'],
  ])('shows %s as an inline alert with its reason', async (state, label, reason) => {
    mockInstances([crew({})])
    mockGrants([grant({ state })])
    renderCard()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(label)
    expect(alert).toHaveTextContent(reason)
    expect(within(alert).getByRole('button', { name: /Ask the agent/ })).toBeTruthy()
  })

  it.each(['connected', 'checking confinement', 'renewing access', 'disconnected', 'unavailable', 'refused',
    'waiting: connected computers is off on the remote gateway', 'run by another process'])
  ('keeps lifecycle state %s as ordinary text', async state => {
    expect(grantFailure(state)).toBe('')
    mockInstances([crew({})])
    mockGrants([grant({ state })])
    renderCard()
    await screen.findByRole('button', { name: 'Stop sharing with Build box' })
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
