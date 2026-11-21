/**
 * The network-setting step in the managed-environment form, asked in the form's network check
 * and never after Launch: while the setting is the restricted default, Launch stays off until a
 * choice there; it writes only on an explicit Agree and before any launch, never on Without or
 * Cancel; the standing "applies to sandboxes you create after this" line; the Restore action.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../ConfirmDialog', () => ({
  useConfirm: () => ({ confirm: vi.fn(async () => true), confirmDialog: null }),
}))

import SandboxLaunchForm, { SANDBOX_PROVISIONER_KIND } from './SandboxLaunchForm'
import { sandboxNetcfgNeedsAsk, sandboxNetcfgSeen } from './SandboxNetworkCard'
import { managedEnvApi } from '../../api/managedEnvApi'
import type { NetcfgStepStatus } from '../../api/managedEnvApi'

const NET_ASK = 'Your network setting blocks the hosts Crew downloads its runtime from.'
const NET_OK = 'Your network setting lets Crew download its runtime.'
const NET_WITHOUT = 'Launching without changing it.'
const APPLIES = 'Applies to sandboxes you create after this; restart does not change it.'

const signedIn = {
  row_lifecycle: true,
  sign_in: { state: 'signed_in', message: 'Signed in to Kiro.', local_agent: 'on' },
} as const
const DIALOG = 'Agreeing opens about 80 well-known hosts for ALL of your sandboxes, not only this one.'

const restricted: NetcfgStepStatus = {
  code: 'status', ok: true, networkAccess: 'INTEGRATIONS_ONLY', customDomains: ['corp.example.com'],
  needsAgreement: true, canRestore: false, dialog: DIALOG,
}

function mount() {
  const launch = vi.fn()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <SandboxLaunchForm
        provisioner={{ id: SANDBOX_PROVISIONER_KIND, kind: SANDBOX_PROVISIONER_KIND } as never}
        launch={launch}
        launching={false}
        disabled={false}
        activeJob={null}
      />
    </QueryClientProvider>,
  )
  return launch
}

function setup(status: NetcfgStepStatus) {
  vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...signedIn })
  vi.spyOn(managedEnvApi, 'sandboxSignedInUser').mockResolvedValue({ email: 'test0@example.com' })
  vi.spyOn(managedEnvApi, 'sandboxList').mockResolvedValue({ sandboxes: [], error: '', row_lifecycle: true })
  vi.spyOn(managedEnvApi, 'netcfgStepStatus').mockResolvedValue(status)
  const apply = vi.spyOn(managedEnvApi, 'netcfgStepApply')
  const restore = vi.spyOn(managedEnvApi, 'netcfgStepRestore')
  return { apply, restore }
}

/** Nothing is asked after Launch: the ask opens from the form's network check
 * ("Allow downloads…"), and Launch stays off until a choice there. */
async function openAsk() {
  fireEvent.click(await screen.findByTestId('sandbox-net-allow'))
}

async function pressCreate() {
  const create = screen.getByTestId('sandbox-create') as HTMLButtonElement
  await waitFor(() => expect(create.disabled).toBe(false))
  await screen.findByTestId('sandbox-netcfg-applies')
  fireEvent.click(create)
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('network setting step', () => {
  it('decides from the server answer only', () => {
    expect(sandboxNetcfgNeedsAsk(restricted)).toBe(true)
    expect(sandboxNetcfgNeedsAsk({ ...restricted, needsAgreement: false })).toBe(false)
    expect(sandboxNetcfgNeedsAsk({ code: 'read_failed', ok: false, message: 'x' })).toBe(false)
    expect(sandboxNetcfgNeedsAsk(undefined)).toBe(false)
    expect(sandboxNetcfgSeen(restricted)).toEqual({ networkAccess: 'INTEGRATIONS_ONLY', customDomains: ['corp.example.com'] })
  })

  it('the check asks, before Launch; Launch stays off and nothing is written until a choice', async () => {
    const { apply } = setup(restricted)
    const launch = mount()
    await waitFor(() => expect(screen.getByTestId('sandbox-check-network-line').textContent).toBe(NET_ASK))
    const create = screen.getByTestId('sandbox-create') as HTMLButtonElement
    expect(create.disabled).toBe(true)
    await openAsk()
    const ask = await screen.findByTestId('sandbox-netcfg-ask')
    expect(ask.textContent).toContain('ALL of your sandboxes')
    expect(screen.getByTestId('sandbox-netcfg-dialog-text').textContent).toBe(DIALOG)
    expect(launch).not.toHaveBeenCalled()
    expect(apply).not.toHaveBeenCalled()
    expect(create.disabled).toBe(true)
  })

  it('Agree writes with what was shown and launches nothing; Launch then creates, asking nothing', async () => {
    const { apply } = setup(restricted)
    const status = vi.mocked(managedEnvApi.netcfgStepStatus)
    apply.mockImplementation(async () => {
      status.mockResolvedValue({ ...restricted, networkAccess: 'COMMON_DEPENDENCIES', needsAgreement: false })
      return { code: 'applied', ok: true, message: 'COMMON_DEPENDENCIES is on for all of your sandboxes.' }
    })
    const launch = mount()
    await openAsk()
    fireEvent.click(await screen.findByTestId('sandbox-netcfg-agree'))
    await waitFor(() => expect(apply).toHaveBeenCalledWith({ networkAccess: 'INTEGRATIONS_ONLY', customDomains: ['corp.example.com'] }))
    expect(launch).not.toHaveBeenCalled()
    expect((await screen.findByTestId('sandbox-netcfg-result')).textContent).toContain('COMMON_DEPENDENCIES')
    await waitFor(() => expect(screen.getByTestId('sandbox-check-network-line').textContent).toBe(NET_OK))
    await pressCreate()
    expect(launch).toHaveBeenCalledWith({ profile: 'sandbox', size_key: '' })
    expect(screen.queryByTestId('sandbox-netcfg-ask')).toBeNull()
  })

  it('a refused or failed apply creates nothing, shows both settings, and Launch stays off', async () => {
    const { apply } = setup(restricted)
    apply.mockResolvedValue({
      code: 'changed_meanwhile', ok: false, message: 'Your network setting changed after the question was shown.',
      shown: { now: { networkAccess: 'INTEGRATIONS_ONLY', customDomains: [] } },
    })
    const launch = mount()
    await openAsk()
    fireEvent.click(await screen.findByTestId('sandbox-netcfg-agree'))
    await screen.findByText(/changed after the question was shown/)
    expect(launch).not.toHaveBeenCalled()
    expect(screen.getByTestId('sandbox-netcfg-result').textContent).toContain('"now"')
    expect((screen.getByTestId('sandbox-create') as HTMLButtonElement).disabled).toBe(true)
  })

  it('a thrown apply creates nothing', async () => {
    const { apply } = setup(restricted)
    apply.mockRejectedValue(new Error('network down'))
    const launch = mount()
    await openAsk()
    fireEvent.click(await screen.findByTestId('sandbox-netcfg-agree'))
    await screen.findByText(/network down/)
    expect(launch).not.toHaveBeenCalled()
  })

  it('Without writes nothing and turns Launch on; Launch then creates with no ask', async () => {
    const { apply } = setup(restricted)
    const launch = mount()
    await openAsk()
    fireEvent.click(await screen.findByTestId('sandbox-netcfg-without'))
    expect(launch).not.toHaveBeenCalled()
    expect(screen.getByTestId('sandbox-check-network-line').textContent).toBe(NET_WITHOUT)
    await pressCreate()
    expect(launch).toHaveBeenCalledWith({ profile: 'sandbox', size_key: '' })
    expect(apply).not.toHaveBeenCalled()
    expect(screen.queryByTestId('sandbox-netcfg-ask')).toBeNull()
  })

  it('Cancel neither writes nor launches, the ask closes, and Launch stays off', async () => {
    const { apply } = setup(restricted)
    const launch = mount()
    await openAsk()
    fireEvent.click(await screen.findByTestId('sandbox-netcfg-cancel'))
    expect(screen.queryByTestId('sandbox-netcfg-ask')).toBeNull()
    expect(launch).not.toHaveBeenCalled()
    expect(apply).not.toHaveBeenCalled()
    expect((screen.getByTestId('sandbox-create') as HTMLButtonElement).disabled).toBe(true)
  })

  it('no choice is preselected: the primary one is not focused', async () => {
    setup(restricted)
    mount()
    await openAsk()
    await screen.findByTestId('sandbox-netcfg-ask')
    expect(document.activeElement?.getAttribute('data-testid')).not.toBe('sandbox-netcfg-agree')
  })

  it.each([
    ['already wider', { ...restricted, networkAccess: 'COMMON_DEPENDENCIES', needsAgreement: false }],
    ['read failed', { code: 'read_failed', ok: false, message: 'could not read' } as NetcfgStepStatus],
  ])('%s: Create launches straight away with no ask and no write', async (_n, status) => {
    const { apply } = setup(status)
    const launch = mount()
    await pressCreate()
    expect(launch).toHaveBeenCalledWith({ profile: 'sandbox', size_key: '' })
    expect(screen.queryByTestId('sandbox-netcfg-ask')).toBeNull()
    expect(apply).not.toHaveBeenCalled()
  })

  it('always shows that it applies to sandboxes created after this', async () => {
    setup({ ...restricted, needsAgreement: false })
    mount()
    expect((await screen.findByTestId('sandbox-netcfg-applies')).textContent).toBe(APPLIES)
  })

  it('Restore shows only with a saved previous setting and calls restore once', async () => {
    const { restore } = setup({ ...restricted, needsAgreement: false, networkAccess: 'COMMON_DEPENDENCIES', canRestore: true })
    restore.mockResolvedValue({ code: 'restored', ok: true, message: 'Your previous network setting is restored.' })
    mount()
    const button = await screen.findByTestId('sandbox-netcfg-restore')
    expect(button.textContent).toBe('Restore network setting')
    fireEvent.click(button)
    await screen.findByText(/previous network setting is restored/)
    expect(restore).toHaveBeenCalledTimes(1)
  })

  it('no Restore button without a saved setting', async () => {
    setup({ ...restricted, canRestore: false })
    mount()
    await screen.findByTestId('sandbox-netcfg-applies')
    expect(screen.queryByTestId('sandbox-netcfg-restore')).toBeNull()
  })
})
