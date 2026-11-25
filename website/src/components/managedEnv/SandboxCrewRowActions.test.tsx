/**
 * The managed-environment lane's form and row actions: drawn only for the lane's own
 * provisioner and rows, actions by state in the row's detail block, Delete asks first,
 * another client's environment gets nothing, and the preview off draws and calls nothing.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const confirmAnswer = { value: true }
const confirmSpy = vi.fn(async () => confirmAnswer.value)
vi.mock('../ConfirmDialog', () => ({
  useConfirm: () => ({ confirm: confirmSpy, confirmDialog: null, cancel: () => {}, confirmOpen: false }),
}))

import type { InstanceView, LaunchJob, RemoteProvisioner } from '../../api/client'
import { managedEnvApi } from '../../api/managedEnvApi'
import type { ManagedEnvItem } from '../../api/managedEnvApi'
import { getCrewRowActions } from '../remoteCrewRowActions'
import { canRenderRemoteProvisionerKind, getRemoteProvisionerRenderer } from '../remoteProvisionerRenderers'
import { PREVIEW_CREW_CLIENT, setPreviewFlag } from '../../utils/previewFlags'
import { SANDBOX_PROVISIONER_ID, SandboxCrewRowDetail, rowState, sandboxManagedBy } from './SandboxCrewRowActions'
import SandboxLaunchForm, { SANDBOX_PROVISIONER_KIND, crewNameOk, resetSandboxNameDraft } from './SandboxLaunchForm'

const HOST = 'crew-sbx-env-001'
const inst = { id: 'r1', name: 'crew-a', ssh_host: HOST, provisioner_id: SANDBOX_PROVISIONER_ID } as InstanceView

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{children}</QueryClientProvider>)
}

/** The row's detail block, as the core draws it while the row is expanded. */
function row() {
  return wrap(<SandboxCrewRowDetail inst={inst} disabled={false} />)
}

const ACTION = /^(Stop|Resume|Delete) environment$/
const actions = () => screen.queryAllByRole('button', { name: ACTION }).map((b) => b.textContent?.trim())

function item(status: string, extra: Partial<ManagedEnvItem> = {}): ManagedEnvItem {
  return { sandbox_id: 'env-001', status, own: true, ssh_host: HOST, ...extra }
}

let listed: ManagedEnvItem[]
let listSpy: ReturnType<typeof vi.spyOn>
let actSpy: ReturnType<typeof vi.spyOn>
let sshPresent: boolean

beforeEach(() => {
  localStorage.clear()
  resetSandboxNameDraft()
  setPreviewFlag(PREVIEW_CREW_CLIENT, true)
  confirmAnswer.value = true
  confirmSpy.mockClear()
  listed = [item('RUNNING')]
  listSpy = vi.spyOn(managedEnvApi, 'list').mockImplementation(async () => ({ sandboxes: listed }))
  actSpy = vi.spyOn(managedEnvApi, 'act').mockImplementation(async () => ({ status: 'STOPPING' }))
  sshPresent = true
  vi.spyOn(managedEnvApi, 'sshConfig').mockImplementation(async () => ({
    present: sshPresent,
    path: '/home/u/.ssh/config',
    block: '# >>> kirocrew crew-sbx >>>\nHost crew-sbx-*\n  User root\n',
  }))
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('the core seed maps', () => {
  it('seed the form for the lane kind and the actions for its rows only', () => {
    expect(canRenderRemoteProvisionerKind(SANDBOX_PROVISIONER_KIND)).toBe(true)
    expect(getRemoteProvisionerRenderer(SANDBOX_PROVISIONER_KIND)?.component).toBe(SandboxLaunchForm)
    expect(getCrewRowActions(inst)?.provisionerId).toBe(SANDBOX_PROVISIONER_ID)
    // No overflow menu: the actions live in the detail block, so the core row keeps its size.
    expect(getCrewRowActions(inst)?.menuItems).toBeUndefined()
    expect(getCrewRowActions({ ...inst, provisioner_id: 'other' })).toBeUndefined()
  })
})

describe('the launch form', () => {
  const provisioner = { id: SANDBOX_PROVISIONER_ID, kind: SANDBOX_PROVISIONER_KIND } as RemoteProvisioner

  it('launches with a valid crew name only', async () => {
    const launch = vi.fn()
    wrap(<SandboxLaunchForm provisioner={provisioner} launch={launch} launching={false} disabled={false} activeJob={null} />)
    const button = screen.getByRole('button', { name: /Launch/ })
    expect(button).toBeDisabled()
    await userEvent.type(screen.getByRole('textbox'), 'Bad--name')
    expect(button).toBeDisabled()
    expect(screen.getByText(/2 to 32 lowercase letters/)).toBeInTheDocument()
    expect(await screen.findByText('Fix the crew name to launch.')).toBeInTheDocument()
    await userEvent.clear(screen.getByRole('textbox'))
    await userEvent.type(screen.getByRole('textbox'), 'crew-a')
    await screen.findByTestId('managed-env-ssh-ready')
    await userEvent.click(button)
    expect(launch).toHaveBeenCalledWith({ profile: 'crew-a', size_key: '' })
  })

  it('holds Launch until the ssh settings are added, which the button does', async () => {
    sshPresent = false
    const addSpy = vi
      .spyOn(managedEnvApi, 'addSshConfig')
      .mockImplementation(async () => ({ present: true, path: '/home/u/.ssh/config' }))
    const launch = vi.fn()
    wrap(<SandboxLaunchForm provisioner={provisioner} launch={launch} launching={false} disabled={false} activeJob={null} />)
    await userEvent.type(screen.getByRole('textbox'), 'crew-a')
    const button = screen.getByRole('button', { name: /Launch/ })
    expect(button).toBeDisabled()
    expect(await screen.findByText('Allow this computer to connect, then launch.')).toBeInTheDocument()
    // the ssh state has loaded: the preview renders only once its block arrives
    await screen.findByTestId('managed-env-ssh-preview')
    expect(screen.getByText(/Adds a few lines to \/home\/u\/\.ssh\/config/)).toBeInTheDocument()
    // the button that unlocks Launch sits beside it
    expect(screen.getByRole('button', { name: /Allow this computer to connect/ }).parentElement).toBe(button.parentElement)
    // the exact lines are there to read before agreeing
    expect(screen.getByTestId('managed-env-ssh-preview').textContent).toContain('Host crew-sbx-*')
    await userEvent.click(await screen.findByRole('button', { name: /Allow this computer to connect/ }))
    expect(addSpy).toHaveBeenCalledTimes(1)
    expect(await screen.findByText(/\/home\/u\/\.ssh\/config has the ssh settings/)).toBeInTheDocument()
    await waitFor(() => expect(button).toBeEnabled())
  })

  it('keeps the typed crew name when the form unmounts, until a launch for it is taken', async () => {
    const form = (job: LaunchJob | null) => (
      <SandboxLaunchForm provisioner={provisioner} launch={vi.fn()} launching={false} disabled={false} activeJob={job} />
    )
    const first = wrap(form(null))
    await userEvent.type(screen.getByRole('textbox'), 'crew-a')
    first.unmount()
    const again = wrap(form(null))
    expect(screen.getByRole('textbox')).toHaveValue('crew-a')
    again.unmount()
    const launched = { id: 'j1', provider_id: SANDBOX_PROVISIONER_ID, profile: 'crew-a' } as LaunchJob
    const done = wrap(form(launched))
    await waitFor(() => expect(screen.getByRole('textbox')).toHaveValue(''))
    done.unmount()
    // That job stays on screen (it failed, say): typing the same name to retry is kept.
    wrap(form({ ...launched, status: 'failed' } as LaunchJob))
    await userEvent.type(screen.getByRole('textbox'), 'crew-a')
    await new Promise((r) => setTimeout(r, 0))
    expect(screen.getByRole('textbox')).toHaveValue('crew-a')
  })

  it('shows a failed ssh write, with no hand-off away from the unsaved name', async () => {
    sshPresent = false
    vi.spyOn(managedEnvApi, 'addSshConfig').mockImplementation(async () => {
      throw new Error('cannot read /home/u/.ssh/config (PermissionError)')
    })
    wrap(<SandboxLaunchForm provisioner={provisioner} launch={vi.fn()} launching={false} disabled={false} activeJob={null} />)
    const allow = await screen.findByRole('button', { name: /Allow this computer to connect/ })
    await waitFor(() => expect(allow).toBeEnabled())
    await userEvent.click(allow)
    expect(await screen.findByText(/Check that you can edit \/home\/u\/\.ssh\/config\. .*PermissionError/)).toBeInTheDocument()
    expect(screen.getByText("Couldn't add the ssh settings")).toBeInTheDocument()
  })

  it.each([
    ['crew-a', true],
    ['a', false],
    ['1crew', false],
    ['crew-', false],
    ['cr--ew', false],
  ])('crew name %s is %s', (name, ok) => {
    expect(crewNameOk(name)).toBe(ok)
  })
})

describe('row actions', () => {
  it.each([
    ['RUNNING', ['Stop environment', 'Delete environment'], 'Environment running'],
    ['STOPPED', ['Resume environment', 'Delete environment'], 'Environment stopped'],
    ['PENDING', ['Delete environment'], 'Environment starting'],
    ['DELETING', ['Delete environment'], 'Environment being deleted'],
    ['FAILED', ['Delete environment'], 'Environment failed'],
  ])('%s shows its state and at most two actions', async (status, expected, line) => {
    listed = [item(status)]
    row()
    expect(await screen.findByText(line)).toBeInTheDocument()
    expect(actions()).toEqual(expected)
  })

  it('shows a failed environment as an error with a hand-off', async () => {
    listed = [item('FAILED')]
    row()
    expect(await screen.findByText(/reports this environment as FAILED/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Ask the agent/i }).length).toBeGreaterThan(0)
  })

  it('stops through the lane route', async () => {
    row()
    await userEvent.click(await screen.findByRole('button', { name: 'Stop environment' }))
    await waitFor(() => expect(actSpy).toHaveBeenCalledWith('env-001', 'stop'))
  })

  it('asks before Delete, deletes only on yes, and leaves the crew row to the core Remove', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    confirmAnswer.value = false
    const view = render(
      <QueryClientProvider client={qc}>
        <SandboxCrewRowDetail inst={inst} disabled={false} />
      </QueryClientProvider>,
    )
    await userEvent.click(await screen.findByRole('button', { name: 'Delete environment' }))
    await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(1))
    expect(actSpy).not.toHaveBeenCalled()
    confirmAnswer.value = true
    await userEvent.click(screen.getByRole('button', { name: 'Delete environment' }))
    await waitFor(() => expect(actSpy).toHaveBeenCalledWith('env-001', 'delete'))
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['managed-env-list'] }))
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ['instances'] })
    view.unmount()
    listed = [item('GONE', { ssh_host: undefined })]
    row()
    expect(await screen.findByText(/Remove the crew from your list/)).toBeInTheDocument()
    expect(actions()).toEqual([])
  })

  it('shows a failed action with a hand-off', async () => {
    actSpy.mockImplementation(async () => {
      throw new Error('The service is pausing requests for 60 s.')
    })
    row()
    await userEvent.click(await screen.findByRole('button', { name: 'Stop environment' }))
    expect(await screen.findByText('The service is pausing requests for 60 s.')).toBeInTheDocument()
    expect(screen.getByText(/Couldn't stop the environment of crew-a/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Ask the agent/i }).length).toBeGreaterThan(0)
  })

  it('shows a later List failure again after an earlier one was dismissed', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    listSpy.mockImplementation(async () => {
      throw new Error('ListSandboxes failed (HTTP 502)')
    })
    render(
      <QueryClientProvider client={qc}>
        <SandboxCrewRowDetail inst={inst} disabled={false} />
      </QueryClientProvider>,
    )
    await screen.findByText('ListSandboxes failed (HTTP 502)')
    await userEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    await waitFor(() => expect(screen.queryByText('ListSandboxes failed (HTTP 502)')).toBeNull())
    listSpy.mockImplementation(async () => ({ sandboxes: listed }))
    await qc.refetchQueries({ queryKey: ['managed-env-list'] })
    await screen.findByText('Environment running')
    listSpy.mockImplementation(async () => {
      throw new Error('ListSandboxes failed (HTTP 503)')
    })
    await qc.refetchQueries({ queryKey: ['managed-env-list'] })
    expect(await screen.findByText('ListSandboxes failed (HTTP 503)')).toBeInTheDocument()
  })

  it('gives another client environment no action', async () => {
    listed = [item('RUNNING', { own: false })]
    row()
    expect(await screen.findByText(/no longer in your managed environments/)).toBeInTheDocument()
    expect(actions()).toEqual([])
  })

  it('draws nothing, calls no route and keeps the default Managed by line while the preview is off', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const tree = () => (
      <QueryClientProvider client={qc}>
        <SandboxCrewRowDetail inst={inst} disabled={false} />
      </QueryClientProvider>
    )
    const view = render(tree())
    await screen.findByRole('button', { name: 'Stop environment' })
    expect(sandboxManagedBy()).toMatch(/^Kiro Managed Environments/)
    setPreviewFlag(PREVIEW_CREW_CLIENT, false)
    listSpy.mockClear()
    view.rerender(tree())
    await waitFor(() => expect(actions()).toEqual([]))
    expect(sandboxManagedBy()).toBeUndefined()
    cleanup()
    row()
    await new Promise((r) => setTimeout(r, 0))
    expect(listSpy).not.toHaveBeenCalled()
    expect(actSpy).not.toHaveBeenCalled()
  })

  it('maps service statuses to six states', () => {
    expect(['PENDING', 'RUNNING', 'STOPPING', 'DELETING', 'GONE', 'WHATEVER'].map(rowState)).toEqual([
      'creating',
      'running',
      'stopped',
      'deleting',
      'gone',
      'error',
    ])
  })
})
