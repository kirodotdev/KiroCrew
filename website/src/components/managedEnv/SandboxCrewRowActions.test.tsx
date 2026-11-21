/**
 * The managed-environment lane's Stop / Resume / Idle timeout / Rename / Delete on its crew rows
 * under "Your crews", drawn on the core's own RemoteCrewPanel through the row-action seam as
 * register.ts registers it, and the launch form's list cut to environments with no crew row.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider, focusManager } from '@tanstack/react-query'

const confirmAnswer = { value: true }
const confirmSpy = vi.fn(async () => confirmAnswer.value)
vi.mock('../ConfirmDialog', () => ({
  useConfirm: () => ({ confirm: confirmSpy, confirmDialog: null, cancel: () => {}, confirmOpen: false }),
}))

vi.mock('../../api/client', () => {
  class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.status = status
      this.body = body
    }
  }
  return {
    ApiError,
    isAuthExpiredError: (e: unknown) =>
      e instanceof ApiError && (e as { authRequired?: boolean }).authRequired === true,
    api: {
      listInstances: vi.fn(),
      addInstance: vi.fn(),
      connectInstance: vi.fn(),
      disconnectInstance: vi.fn(),
      removeInstance: vi.fn(),
      instanceStatus: vi.fn(),
      updateInstance: vi.fn(),
      patchConfig: vi.fn(),
      cloudLaunches: vi.fn(),
      cloudPreflight: vi.fn(),
      cloudProvisioners: vi.fn(),
      cloudIamPolicy: vi.fn(),
      cloudLaunch: vi.fn(),
      cloudIdentity: vi.fn(),
      cloudLaunchStatus: vi.fn(),
      cloudLaunchCancel: vi.fn(),
      cloudLaunchSignin: vi.fn(),
      cloudStop: vi.fn(),
      cloudStart: vi.fn(),
      cloudDestroy: vi.fn(),
    },
  }
})

import { api } from '../../api/client'
import type { InstanceView } from '../../api/client'
import { getCrewRowActions } from '../remoteCrewRowActions'
import { RemoteCrewPanel } from '../../pages/settings/RemoteCrewPanel'
import { renderWithProviders } from '../../test/helpers'
import { BUILTIN_PROVISIONER_ID } from '../../utils/remoteCrew'
// The registration under test, as the dashboard loads it.
import './register'
import { managedEnvApi } from '../../api/managedEnvApi'
import type { SandboxConnectPhase, SandboxItem, SandboxList, SandboxSignIn } from '../../api/managedEnvApi'
import {
  SANDBOX_PROVISIONER_ID,
  SandboxCrewRowDetail,
  SandboxCrewRowMenuItems,
  sandboxIdFromHost,
} from './SandboxCrewRowActions'
import SandboxLaunchForm, {
  SANDBOX_LIST_QUERY_KEY,
  SANDBOX_PROVISIONER_KIND,
  SANDBOX_ROW_LIFECYCLE_QUERY_KEY,
} from './SandboxLaunchForm'
import { withCrewPointer } from './text'

const LIST_KEY = JSON.stringify(SANDBOX_LIST_QUERY_KEY)
const CREWS_KEY = JSON.stringify(['instances'])
const STOPPED_LINE = 'Sandbox stopped. Resume it to use this crew.'

function crew(id: string, name: string, extra: Partial<InstanceView> = {}): InstanceView {
  return {
    id, name, ssh_host: `crew-sbx-sb-${id}`, remote_port: 5476, local_port: 0, ttl: '20h', remote_bin: '',
    connection_method: 'ssh', ssm_target: '', aws_profile: '', aws_region: '', ssm_run_as: '',
    provisioner_id: SANDBOX_PROVISIONER_ID, was_connected: false,
    status: { instance_id: id, state: 'disconnected' } as InstanceView['status'],
    ...extra,
  }
}

function box(sid: string, status: string, instanceId: string, extra: Partial<SandboxItem> = {}): SandboxItem {
  return {
    sandbox_id: sid, status, display_name: `kc-${sid}`, created_at: 1, instance_id: instanceId,
    name: instanceId ? `crew-${instanceId}` : '', ssh_host: `crew-sbx-${sid}`, action: null, own: true, ...extra,
  }
}

let listAnswer: SandboxList
let crews: InstanceView[]
let signinAnswer: SandboxSignIn = { state: 'signed_in', message: '', local_agent: 'on' }
const busy = { action: 'stop', running: true, error: '', at: 1 }

/** The list answer the lane sends: `row_lifecycle` is on whenever the lane answers at all. */
function answer(sandboxes: SandboxItem[]): SandboxList {
  return { sandboxes, error: '', row_lifecycle: true }
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [] } as never)
  vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] } as never)
  vi.mocked(api.listInstances).mockImplementation(async () => ({ active: true, warm_set_cap: 5, instances: crews }) as never)
  vi.spyOn(managedEnvApi, 'sandboxList').mockImplementation(async () => listAnswer)
  signinAnswer = { state: 'signed_in', message: 'Signed in to Kiro.', local_agent: 'on' }
  vi.spyOn(managedEnvApi, 'sandboxSignedInUser').mockImplementation(async () => ({ email: 'test0@example.com' }))
  vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockImplementation(async () => ({
    row_lifecycle: true,
    sign_in: { ...signinAnswer },
  }))
  vi.mocked(api.connectInstance).mockResolvedValue({ instance_id: 'x', state: 'connected' } as never)
  vi.mocked(api.disconnectInstance).mockResolvedValue({ disconnected: 'x', was_connected: true } as never)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  confirmSpy.mockClear()
  confirmAnswer.value = true
})

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/** Render the panel and open every crew row's details: the lane's detail block and the row
 * menu are drawn inside them. */
async function renderPanel() {
  const out = renderWithProviders(<RemoteCrewPanel />)
  const u = userEvent.setup()
  for (const c of crews) {
    const toggle = await screen.findByRole('button', { name: new RegExp(`^(Show|Hide) details for ${escapeRe(c.name)}$`) })
    if (toggle.getAttribute('aria-expanded') !== 'true') await u.click(toggle)
  }
  return out
}

async function openMenu(u: ReturnType<typeof userEvent.setup>, name: string) {
  await u.click(await screen.findByRole('button', { name: new RegExp(`More actions for ${name}`, 'i') }))
}

const items = (name: string) => ({
  stop: screen.queryByRole('menuitem', { name: `Stop sandbox of ${name}` }),
  resume: screen.queryByRole('menuitem', { name: `Resume sandbox of ${name}` }),
  delete: screen.queryByRole('menuitem', { name: `Delete sandbox of ${name}` }),
})

/** The lane is off: every route answers 404. */
function laneOff() {
  vi.mocked(managedEnvApi.sandboxRowLifecycle).mockRejectedValue(new Error('not found'))
}

describe('registration', () => {
  it('register.ts registers the row actions under the lane stamp', () => {
    const got = getCrewRowActions(crew('c1', 'box'))
    expect(got?.provisionerId).toBe('kiro_managed_env')
    expect(got?.menuItems).toBe(SandboxCrewRowMenuItems)
    expect(got?.detail).toBe(SandboxCrewRowDetail)
  })

  it('the seam gives nothing to another stamp, the built-in one, or a chained row', () => {
    expect(getCrewRowActions(crew('c1', 'box', { provisioner_id: 'other_lane' }))).toBeUndefined()
    expect(getCrewRowActions(crew('c1', 'box', { provisioner_id: BUILTIN_PROVISIONER_ID }))).toBeUndefined()
    expect(getCrewRowActions(crew('c1', 'box', { provisioner_id: undefined }))).toBeUndefined()
    expect(getCrewRowActions(crew('c1', 'box', { via_instance_id: 'c0' }))).toBeUndefined()
  })
})

describe('on the crew row', () => {
  it('offers Stop / Resume by status, Delete except PENDING, GONE and busy', async () => {
    crews = [crew('c1', 'run-a'), crew('c2', 'stop-b'), crew('c3', 'pend-c'), crew('c4', 'gone-d'), crew('c5', 'busy-e')]
    listAnswer = answer([
      box('sb-1', 'RUNNING', 'c1'), box('sb-2', 'STOPPED', 'c2'), box('sb-3', 'PENDING', 'c3'),
      box('sb-4', 'GONE', 'c4'), box('sb-5', 'RUNNING', 'c5', { action: busy }),
    ])
    const u = userEvent.setup()
    await renderPanel()

    expect((await screen.findByTestId('sandbox-row-state-c1')).textContent).toBe('Sandbox running')
    expect(screen.getByTestId('sandbox-row-state-c2').textContent).toBe(STOPPED_LINE)
    expect(screen.getByTestId('sandbox-row-state-c3').textContent).toBe('Sandbox starting…')
    expect(screen.getByTestId('sandbox-row-state-c4').textContent).toBe('Sandbox deleted')
    expect(screen.getByTestId('sandbox-row-state-c5').textContent).toBe('Stopping…')
    // the detail block sits on its own row
    expect(screen.getByTestId('sandbox-row-detail-c2').closest('[data-crew-id]')?.getAttribute('data-crew-id')).toBe('c2')

    const want: Record<string, [boolean, boolean, boolean]> = {
      'run-a': [true, false, true], 'stop-b': [false, true, true], 'pend-c': [false, false, false],
      'gone-d': [false, false, false], 'busy-e': [false, false, false],
    }
    for (const [name, [stop, resume, del]] of Object.entries(want)) {
      await openMenu(u, name)
      const got = items(name)
      expect([Boolean(got.stop), Boolean(got.resume), Boolean(got.delete)], name).toEqual([stop, resume, del])
      await u.keyboard('{Escape}')
      // the core's own Remove is still there beside ours
      expect(screen.getByRole('button', { name: new RegExp(`Remove ${name}`, 'i') })).toBeTruthy()
    }
  })

  it('the row menu names the signed-in account, and only while signed in', async () => {
    crews = [crew('c1', 'run-a')]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1')])
    const u = userEvent.setup()
    await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')
    await openMenu(u, 'run-a')
    const line = await screen.findByTestId('sandbox-row-signed-in-c1')
    await waitFor(() => expect(line.textContent).toBe('Signed in to Kiro as test0@example.com.'))
  })

  it('Stop and Resume post straight away to the row sandbox id', async () => {
    crews = [crew('c1', 'run-a'), crew('c2', 'stop-b')]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1'), box('sb-2', 'STOPPED', 'c2')])
    const post = vi.spyOn(managedEnvApi, 'sandboxAction').mockResolvedValue({ ok: true, action: 'x' })
    const u = userEvent.setup()
    await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')

    await openMenu(u, 'run-a')
    await u.click(items('run-a').stop as HTMLElement)
    await waitFor(() => expect(post).toHaveBeenCalledWith('sb-1', 'stop'))
    await openMenu(u, 'stop-b')
    await u.click(items('stop-b').resume as HTMLElement)
    await waitFor(() => expect(post).toHaveBeenCalledWith('sb-2', 'resume'))
    expect(post).toHaveBeenCalledTimes(2)
    expect(confirmSpy).not.toHaveBeenCalled()
  })

  it('the row shows Stop, Resume and Idle timeout as buttons by state, and they post as the menu does', async () => {
    crews = [crew('c1', 'run-a'), crew('c2', 'stop-b'), crew('c3', 'pend-c'), crew('c4', 'gone-d')]
    listAnswer = answer([
      box('sb-1', 'RUNNING', 'c1'),
      box('sb-2', 'STOPPED', 'c2'),
      box('sb-3', 'PENDING', 'c3'),
      box('sb-4', 'GONE', 'c4'),
    ])
    const post = vi.spyOn(managedEnvApi, 'sandboxAction').mockResolvedValue({ ok: true, action: 'x' })
    const u = userEvent.setup()
    await renderPanel()
    await screen.findByTestId('sandbox-row-buttons-c1')
    const btn = (k: string, id: string) => screen.queryByTestId(`sandbox-row-${k}-btn-${id}`)
    // RUNNING: Stop and Idle timeout; STOPPED: Resume and Idle timeout; PENDING: Idle only; GONE: none
    expect([btn('stop', 'c1'), btn('resume', 'c1'), btn('idle', 'c1')].map(Boolean)).toEqual([true, false, true])
    expect([btn('stop', 'c2'), btn('resume', 'c2'), btn('idle', 'c2')].map(Boolean)).toEqual([false, true, true])
    expect([btn('stop', 'c3'), btn('resume', 'c3'), btn('idle', 'c3')].map(Boolean)).toEqual([false, false, true])
    expect(screen.queryByTestId('sandbox-row-buttons-c4')).toBeNull()
    // the state line stays beside them; Delete is not a row button
    expect(screen.getByTestId('sandbox-row-state-c1').textContent).toBe('Sandbox running')
    expect(screen.queryByTestId('sandbox-row-delete-btn-c1')).toBeNull()
    expect(btn('stop', 'c1')?.textContent).toBe('Stop')
    expect(btn('idle', 'c1')?.textContent).toBe('Idle timeout…')

    await u.click(btn('stop', 'c1') as HTMLElement)
    await waitFor(() => expect(post).toHaveBeenCalledWith('sb-1', 'stop'))
    await u.click(btn('resume', 'c2') as HTMLElement)
    await waitFor(() => expect(post).toHaveBeenCalledWith('sb-2', 'resume'))
    expect(post).toHaveBeenCalledTimes(2)
    expect(confirmSpy).not.toHaveBeenCalled()
  })

  it('no row buttons on a sandbox another client made, or while an action runs', async () => {
    crews = [crew('c1', 'other-a'), crew('c2', 'busy-b')]
    listAnswer = answer([
      box('sb-1', 'RUNNING', 'c1', { own: false }),
      box('sb-2', 'RUNNING', 'c2', { action: { ...busy } }),
    ])
    await renderPanel()
    await screen.findByTestId('sandbox-row-state-c2')
    expect(screen.queryByTestId('sandbox-row-buttons-c1')).toBeNull()
    expect(screen.queryByTestId('sandbox-row-buttons-c2')).toBeNull()
  })

  it('Delete asks first; confirmed, it posts, and once the list drops the row the crew row goes', async () => {
    crews = [crew('c1', 'run-a'), crew('c2', 'keep-b')]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1'), box('sb-2', 'RUNNING', 'c2')])
    const post = vi.spyOn(managedEnvApi, 'sandboxAction').mockImplementation(async () => {
      // the server's worker: the delete runs on, then drops the crew row
      listAnswer = answer([box('sb-1', 'DELETING', 'c1', { action: { action: 'delete', running: true, error: '', at: 1 } }),
        box('sb-2', 'RUNNING', 'c2')])
      return { ok: true, action: 'delete' }
    })
    const u = userEvent.setup()
    await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')

    confirmAnswer.value = false
    await openMenu(u, 'run-a')
    await u.click(items('run-a').delete as HTMLElement)
    await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(1))
    expect(post).not.toHaveBeenCalled()

    confirmAnswer.value = true
    await openMenu(u, 'run-a')
    await u.click(items('run-a').delete as HTMLElement)
    await waitFor(() => expect(post).toHaveBeenCalledWith('sb-1', 'delete'))
    expect(await screen.findByText('Deleting…')).toBeTruthy()
    const before = vi.mocked(api.listInstances).mock.calls.length

    // the sandbox is gone and its crew row with it
    crews = [crew('c2', 'keep-b')]
    listAnswer = answer([box('sb-2', 'RUNNING', 'c2')])
    await waitFor(() => expect(vi.mocked(api.listInstances).mock.calls.length).toBeGreaterThan(before), { timeout: 5_000 })
    await waitFor(() => expect(document.querySelector('[data-crew-id="c1"]')).toBeNull())
    expect(document.querySelector('[data-crew-id="c2"]')).not.toBeNull()
  })

  it('shows the last action error, and a route refusal', async () => {
    crews = [crew('c1', 'stop-b')]
    listAnswer = answer([box('sb-1', 'STOPPED', 'c1', { action: { action: 'resume', running: false, error: 'resume failed upstream', at: 1 } })])
    vi.spyOn(managedEnvApi, 'sandboxAction').mockRejectedValue(new Error('another action is still running on this sandbox'))
    const u = userEvent.setup()
    await renderPanel()
    expect((await screen.findByTestId('sandbox-row-error-c1')).textContent).toContain('resume failed upstream')
    await openMenu(u, 'stop-b')
    await u.click(items('stop-b').resume as HTMLElement)
    expect(await screen.findByText('another action is still running on this sandbox')).toBeTruthy()
  })

  it('a crew row whose sandbox another client made offers nothing (no Delete, no Idle timeout)', async () => {
    crews = [crew('c1', 'odd-a')]
    listAnswer = answer([box('sb-9', 'RUNNING', 'c1', { own: false })])
    const post = vi.spyOn(managedEnvApi, 'sandboxAction')
    const u = userEvent.setup()
    await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')
    await openMenu(u, 'odd-a')
    expect(items('odd-a')).toEqual({ stop: null, resume: null, delete: null })
    expect(screen.queryByRole('menuitem', { name: 'Idle timeout of odd-a' })).toBeNull()
    expect(post).not.toHaveBeenCalled()
  })

  it('a RUNNING sandbox whose crew row is in error gets a plain line; a STOPPED one does not', async () => {
    const failing = { instance_id: 'x', state: 'error', error: 'ssh exited 255: mint-and-attach: Sign in to connect.' }
    crews = [
      crew('c1', 'run-a', { status: { ...failing, instance_id: 'c1' } as InstanceView['status'] }),
      crew('c2', 'stop-b', { status: { ...failing, instance_id: 'c2' } as InstanceView['status'] }),
    ]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1'), box('sb-2', 'STOPPED', 'c2')])
    await renderPanel()
    expect((await screen.findByTestId('sandbox-row-unreachable-c1')).textContent).toBe(
      "Can't reach this crew yet. If the sandbox just started, wait a minute, then Connect.",
    )
    expect(screen.getByTestId('sandbox-row-state-c2').textContent).toBe(STOPPED_LINE)
    expect(screen.queryByTestId('sandbox-row-unreachable-c2')).toBeNull()
  })

  it("with the client stopped, a crew whose connect failed says it can't reconnect until a restart", async () => {
    const failed = { instance_id: 'x', state: 'error', error: "the crew's token could not be confirmed or re-minted; check that its remote gateway is running" }
    crews = [
      crew('c1', 'down-a', { status: { ...failed, instance_id: 'c1' } as InstanceView['status'] }),
      crew('c2', 'idle-b'),
    ]
    const plain = "Can't reach the sandbox service right now. Restart the dashboard to try again."
    listAnswer = { ...answer([box('sb-1', '', 'c1'), box('sb-2', '', 'c2')]), error: plain, client_stopped: true }
    await renderPanel()
    expect((await screen.findByTestId('sandbox-row-cant-reconnect-c1')).textContent).toBe(
      "This crew can't reconnect until the dashboard restarts.",
    )
    expect(screen.queryByTestId('sandbox-row-cant-reconnect-c2')).toBeNull() // no failed connect
  })

  it('without the client stopped, a failed connect gets no reconnect line', async () => {
    const failed = { instance_id: 'x', state: 'error', error: 'ssh exited 255' }
    crews = [crew('c1', 'run-a', { status: { ...failed, instance_id: 'c1' } as InstanceView['status'] })]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1')])
    await renderPanel()
    await screen.findByTestId('sandbox-row-unreachable-c1')
    expect(screen.queryByTestId('sandbox-row-cant-reconnect-c1')).toBeNull()
  })

  it('a box refused by the policy shows its one plain line; the other box and the state read on', async () => {
    const policy =
      "Your organization's policy doesn't allow this for your account yet. Ask your Kiro admin to allow managed environments."
    const failed = { instance_id: 'x', state: 'error', error: `ssh exited 255: mint-and-attach: ${policy}` }
    crews = [
      crew('c1', 'deny-a', { status: { ...failed, instance_id: 'c1' } as InstanceView['status'] }),
      crew('c2', 'run-b', { status: { instance_id: 'c2', state: 'connected' } as InstanceView['status'] }),
    ]
    listAnswer = answer([{ ...box('sb-1', 'RUNNING', 'c1'), policy_line: policy }, box('sb-2', 'RUNNING', 'c2')])
    await renderPanel()
    expect((await screen.findByTestId('sandbox-row-policy-c1')).textContent).toBe(policy)
    expect(screen.getByTestId('sandbox-row-state-c1').textContent).toBe('Sandbox running')
    expect(screen.getByTestId('sandbox-row-state-c2').textContent).toBe('Sandbox running')
    // the one line: no "can't reach", no "gateway isn't running", no reconnect line
    expect(screen.queryByTestId('sandbox-row-unreachable-c1')).toBeNull()
    expect(screen.queryByTestId('sandbox-row-gateway-down-c1')).toBeNull()
    expect(screen.queryByTestId('sandbox-row-cant-reconnect-c1')).toBeNull()
    expect(screen.queryByTestId('sandbox-row-policy-c2')).toBeNull()
    expect(document.body.textContent).not.toContain('Sandbox state unknown')
  })

  it('a Connected row loses the policy line at the next list read once the box is served, with no reload', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const policy =
        "Your organization's policy doesn't allow this for your account yet. Ask your Kiro admin to allow managed environments."
      // the tunnel is up, its refreshes refused by the policy
      crews = [crew('c1', 'deny-a', { status: { instance_id: 'c1', state: 'connected' } as InstanceView['status'] })]
      listAnswer = answer([{ ...box('sb-1', 'RUNNING', 'c1'), policy_line: policy }])
      await renderPanel()
      expect((await screen.findByTestId('sandbox-row-policy-c1')).textContent).toBe(policy)
      const reads = vi.mocked(managedEnvApi.sandboxList).mock.calls.length
      // a call for that box succeeded: the server's next list answer has no line for it
      listAnswer = answer([box('sb-1', 'RUNNING', 'c1')])
      await vi.advanceTimersByTimeAsync(31_000) // the steady list poll (30 s), not a reload
      expect(vi.mocked(managedEnvApi.sandboxList).mock.calls.length).toBeGreaterThan(reads)
      await waitFor(() => expect(screen.queryByTestId('sandbox-row-policy-c1')).toBeNull())
      expect(document.body.textContent).not.toContain(policy)
      expect(screen.getByTestId('sandbox-row-state-c1').textContent).toBe('Sandbox running')
    } finally {
      vi.useRealTimers()
    }
  })

  it('a STOPPED crew the lane turned off reads Stopped with Resume, no Error and no ssh text', async () => {
    const held = { instance_id: 'x', state: 'error', error: 'ssh exited 255: mint-and-attach: Stopped. Resume it, then Connect.' }
    crews = [
      // the lane's turn-off: the row off, the last held try's error still in its status
      crew('c1', 'stop-a', { disabled: true, status: { ...held, instance_id: 'c1' } as InstanceView['status'] }),
      // the same row not turned off: the core's own badge shows the held try
      crew('c2', 'stop-b', { status: { ...held, instance_id: 'c2' } as InstanceView['status'] }),
      crew('c3', 'run-c', { status: { instance_id: 'c3', state: 'connected' } as InstanceView['status'] }),
    ]
    listAnswer = answer([box('sb-1', 'STOPPED', 'c1'), box('sb-2', 'STOPPED', 'c2'), box('sb-3', 'RUNNING', 'c3')])
    await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')
    const row = (id: string) => document.querySelector(`[data-crew-id="${id}"]`)?.textContent ?? ''
    expect(screen.getByTestId('sandbox-row-state-c1').textContent).toBe(STOPPED_LINE)
    expect(screen.getByTestId('sandbox-row-resume-btn-c1')).toBeTruthy()
    expect(screen.queryByTestId('sandbox-row-stop-btn-c1')).toBeNull()
    expect(row('c1')).not.toContain('ssh exited 255')
    expect(row('c1')).not.toMatch(/\bError\b/)
    expect(row('c2')).toContain('ssh exited 255') // the control: this test sees the core's badge
    // the running crew's row is unchanged
    expect(screen.getByTestId('sandbox-row-state-c3').textContent).toBe('Sandbox running')
    expect(screen.getByTestId('sandbox-row-stop-btn-c3')).toBeTruthy()
    expect(api.connectInstance).not.toHaveBeenCalledWith('c1')
  })

  it('a chained row with the lane stamp and a matching id gets nothing', async () => {
    crews = [crew('c0', 'parent', { provisioner_id: undefined }), crew('c1', 'riding', { via_instance_id: 'c0' })]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1')])
    await renderPanel()
    expect(screen.queryByRole('button', { name: 'More actions for riding' })).toBeNull()
    expect(items('riding')).toEqual({ stop: null, resume: null, delete: null })
    expect(screen.queryByTestId('sandbox-row-detail-c1')).toBeNull()
  })
})

describe('lane off (every route answers 404)', () => {
  it('the row draws nothing of ours and Your crews never calls the list', async () => {
    crews = [crew('c1', 'run-a')]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1')])
    laneOff()
    const u = userEvent.setup()
    await renderPanel()
    await waitFor(() => expect(managedEnvApi.sandboxRowLifecycle).toHaveBeenCalled())
    await openMenu(u, 'run-a')
    expect(items('run-a')).toEqual({ stop: null, resume: null, delete: null })
    expect(screen.queryByTestId('sandbox-row-detail-c1')).toBeNull()
    expect(screen.queryByText('Sandbox running')).toBeNull()
    expect(managedEnvApi.sandboxList).not.toHaveBeenCalled()
  })

  it('35 s later (fake time) the list is still never called, and the switch is read once', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      crews = [crew('c1', 'run-a'), crew('c2', 'stop-b')]
      listAnswer = answer([box('sb-1', 'RUNNING', 'c1'), box('sb-2', 'STOPPED', 'c2')])
      laneOff()
      await renderPanel()
      await screen.findByRole('button', { name: /More actions for run-a/i })
      await waitFor(() => expect(managedEnvApi.sandboxRowLifecycle).toHaveBeenCalled())
      const switchReads = vi.mocked(managedEnvApi.sandboxRowLifecycle).mock.calls.length
      await vi.advanceTimersByTimeAsync(35_000)
      expect(managedEnvApi.sandboxList).not.toHaveBeenCalled()
      expect(vi.mocked(managedEnvApi.sandboxRowLifecycle).mock.calls.length).toBe(switchReads)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('connections follow the sandbox', () => {
  const failed = (id: string) =>
    ({ instance_id: id, state: 'error', error: 'ssh exited 255: mint-and-attach: Sign in to connect.' }) as InstanceView['status']

  it('when the sign-in lands: one connect for a RUNNING row in error, none for STOPPED or connected', async () => {
    signinAnswer = { ...signinAnswer, state: 'signin_required' }
    crews = [
      crew('c1', 'run-a', { status: failed('c1') }),
      crew('c2', 'stop-b', { status: failed('c2') }),
      crew('c3', 'up-c', { status: { instance_id: 'c3', state: 'connected' } as InstanceView['status'] }),
    ]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1'), box('sb-2', 'STOPPED', 'c2'), box('sb-3', 'RUNNING', 'c3')])
    const { queryClient } = await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')
    await waitFor(() => expect(managedEnvApi.sandboxRowLifecycle).toHaveBeenCalled())
    expect(api.connectInstance).not.toHaveBeenCalled()
    signinAnswer = { ...signinAnswer, state: 'signed_in' }
    await queryClient.invalidateQueries({ queryKey: SANDBOX_ROW_LIFECYCLE_QUERY_KEY })
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('c1'))
    // more renders and list reads bring no second connect
    const listReads = vi.mocked(managedEnvApi.sandboxList).mock.calls.length
    await queryClient.invalidateQueries({ queryKey: SANDBOX_LIST_QUERY_KEY })
    await waitFor(() => expect(vi.mocked(managedEnvApi.sandboxList).mock.calls.length).toBeGreaterThan(listReads))
    expect(vi.mocked(api.connectInstance).mock.calls).toEqual([['c1']])
  })

  it('a row that goes connecting and then error again after its one connect gets no second connect', async () => {
    crews = [crew('c1', 'run-a', { status: failed('c1') })]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1')])
    const { queryClient } = await renderPanel()
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('c1'))
    // the connect is under way: the core row reads connecting, with no error
    crews = [crew('c1', 'run-a', { status: { instance_id: 'c1', state: 'connecting' } as InstanceView['status'] })]
    await queryClient.invalidateQueries({ queryKey: ['instances'] })
    await waitFor(() => expect(vi.mocked(api.listInstances).mock.calls.length).toBeGreaterThan(1))
    // and it fails again
    const before = vi.mocked(api.listInstances).mock.calls.length
    crews = [crew('c1', 'run-a', { status: failed('c1') })]
    await queryClient.invalidateQueries({ queryKey: ['instances'] })
    await waitFor(() => expect(vi.mocked(api.listInstances).mock.calls.length).toBeGreaterThan(before))
    await screen.findByTestId('sandbox-row-unreachable-c1')
    expect(vi.mocked(api.connectInstance).mock.calls).toEqual([['c1']])
  })

  it("the row's Stop disconnects first; its Resume connects once RUNNING", async () => {
    crews = [crew('c1', 'run-a', { status: { instance_id: 'c1', state: 'connected' } as InstanceView['status'] })]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1')])
    const post = vi.spyOn(managedEnvApi, 'sandboxAction').mockResolvedValue({ ok: true, action: 'x' })
    const u = userEvent.setup()
    const { queryClient } = await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')

    await openMenu(u, 'run-a')
    await u.click(items('run-a').stop as HTMLElement)
    await waitFor(() => expect(post).toHaveBeenCalledWith('sb-1', 'stop'))
    expect(api.disconnectInstance).toHaveBeenCalledWith('c1')
    const order = (f: unknown) => vi.mocked(f as () => void).mock.invocationCallOrder[0]
    expect(order(api.disconnectInstance)).toBeLessThan(order(post))

    // now STOPPED and disconnected; Resume, and the sandbox comes back RUNNING
    crews = [crew('c1', 'run-a')]
    listAnswer = answer([box('sb-1', 'STOPPED', 'c1')])
    await queryClient.invalidateQueries({ queryKey: SANDBOX_LIST_QUERY_KEY })
    await queryClient.invalidateQueries({ queryKey: ['instances'] })
    await waitFor(() => expect(screen.getByTestId('sandbox-row-state-c1').textContent).toBe(STOPPED_LINE))
    expect(api.connectInstance).not.toHaveBeenCalled()
    await openMenu(u, 'run-a')
    await u.click(items('run-a').resume as HTMLElement)
    await waitFor(() => expect(post).toHaveBeenCalledWith('sb-1', 'resume'))
    expect(api.connectInstance).not.toHaveBeenCalled()
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1')])
    await queryClient.invalidateQueries({ queryKey: SANDBOX_LIST_QUERY_KEY })
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('c1'))
    expect(api.connectInstance).toHaveBeenCalledTimes(1)
  })
})

describe('a first list read that failed recovers with no reload', () => {
  // the list fails until the sign-in completes
  function failingUntilSignedIn() {
    const state = { signedIn: false }
    vi.mocked(managedEnvApi.sandboxList).mockImplementation(async () => {
      if (!state.signedIn) throw new Error('not signed in')
      return listAnswer
    })
    return state
  }

  it('at the next poll (30 s of fake time)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      crews = [crew('c1', 'run-a')]
      listAnswer = answer([box('sb-1', 'RUNNING', 'c1')])
      const state = failingUntilSignedIn()
      await renderPanel()
      await waitFor(() => expect(managedEnvApi.sandboxList).toHaveBeenCalledTimes(1))
      expect(screen.queryByTestId('sandbox-row-detail-c1')).toBeNull()
      state.signedIn = true
      await vi.advanceTimersByTimeAsync(30_000)
      expect((await screen.findByTestId('sandbox-row-state-c1')).textContent).toBe('Sandbox running')
    } finally {
      vi.useRealTimers()
    }
  })

  it('on window focus', async () => {
    crews = [crew('c1', 'run-a')]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1')])
    const state = failingUntilSignedIn()
    await renderPanel()
    await waitFor(() => expect(managedEnvApi.sandboxList).toHaveBeenCalledTimes(1))
    expect(screen.queryByTestId('sandbox-row-detail-c1')).toBeNull()
    state.signedIn = true
    try {
      act(() => {
        focusManager.setFocused(false)
        focusManager.setFocused(true)
      })
      expect((await screen.findByTestId('sandbox-row-state-c1')).textContent).toBe('Sandbox running')
    } finally {
      focusManager.setFocused(undefined)
    }
  })
})

describe('Delete re-checks the row after its confirm', () => {
  it.each([
    ['busy', { action: { action: 'stop', running: true, error: '', at: 2 } }, 'RUNNING'],
    ['PENDING', {}, 'PENDING'],
  ] as const)('the sandbox turned %s while the confirm was open: no post', async (_label, extra, status) => {
    crews = [crew('c1', 'run-a')]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1')])
    const post = vi.spyOn(managedEnvApi, 'sandboxAction').mockResolvedValue({ ok: true, action: 'delete' })
    let answerConfirm: (ok: boolean) => void = () => {}
    confirmSpy.mockImplementationOnce(() => new Promise<boolean>(resolve => { answerConfirm = resolve }))
    const u = userEvent.setup()
    const { queryClient } = await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')

    await openMenu(u, 'run-a')
    await u.click(items('run-a').delete as HTMLElement)
    await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(1))
    // while the question is open, the list moves on
    listAnswer = answer([box('sb-1', status, 'c1', extra)])
    await queryClient.invalidateQueries({ queryKey: SANDBOX_LIST_QUERY_KEY })
    await waitFor(() => expect(screen.getByTestId('sandbox-row-state-c1').textContent).not.toBe('Sandbox running'))
    // the yes arrives late; act() flushes the handler's continuation before the check
    await act(async () => {
      answerConfirm(true)
    })
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(post).not.toHaveBeenCalled()
  })
})

function mountForm() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <SandboxLaunchForm
        provisioner={{ id: SANDBOX_PROVISIONER_KIND, kind: SANDBOX_PROVISIONER_KIND } as never}
        launch={vi.fn()} launching={false} disabled={false} activeJob={null}
      />
    </QueryClientProvider>,
  )
}

describe("the form's list", () => {
  it('only sandboxes with no crew row, own with buttons, another client with the not-set-up line', async () => {
    listAnswer = answer([
      box('sb-1', 'RUNNING', 'c1'), // has a crew row: on the row, not here
      box('sb-2', 'STOPPED', ''), // own, its row removed
      box('sb-3', 'RUNNING', '', { own: false }), // another client's
    ])
    const post = vi.spyOn(managedEnvApi, 'sandboxAction')
    mountForm()
    await screen.findByTestId('sandbox-row-sb-2')
    expect(screen.getByText('Sandboxes without a crew')).toBeTruthy()
    expect(screen.queryByText('Your sandboxes')).toBeNull()
    expect(screen.queryByTestId('sandbox-row-sb-1')).toBeNull()
    expect(screen.getByTestId('sandbox-resume-sb-2')).toBeTruthy()
    expect(screen.getByTestId('sandbox-delete-sb-2')).toBeTruthy()
    expect(screen.getByTestId('sandbox-not-set-up-sb-3').textContent).toBe("Crew didn't set this sandbox up.")
    expect(screen.queryByTestId('sandbox-other-client-sb-3')).toBeNull()
    for (const k of ['stop', 'resume', 'delete']) expect(screen.queryByTestId(`sandbox-${k}-sb-3`)).toBeNull()
    expect(post).not.toHaveBeenCalled()
  })

  it('a kc- box from another dashboard is the marker group; an own one keeps its buttons', async () => {
    listAnswer = answer([
      box('sb-m', 'STOPPED', '', { own: false, display_name: 'kc-0a1b2c', crew_marker: true }),
      box('sb-ide', 'RUNNING', '', { own: false, display_name: 'ide', crew_marker: false }),
      box('sb-2', 'STOPPED', '', { display_name: 'kc-abcdef', crew_marker: true }), // own, row gone
    ])
    const post = vi.spyOn(managedEnvApi, 'sandboxAction')
    mountForm()
    const group = await screen.findByTestId('sandbox-marker-group')
    expect([...group.querySelectorAll('li')].map(li => li.getAttribute('data-testid'))).toEqual(['sandbox-row-sb-m'])
    expect(screen.getByTestId('sandbox-status-sb-m').textContent).toBe('STOPPED')
    expect(screen.getByTestId('sandbox-marker-sb-m').textContent).toBe('Made by Crew on another dashboard.')
    expect(screen.getByTestId('sandbox-not-set-up-sb-ide').textContent).toBe("Crew didn't set this sandbox up.")
    for (const id of ['sb-m', 'sb-ide']) {
      for (const k of ['stop', 'resume', 'delete']) expect(screen.queryByTestId(`sandbox-${k}-${id}`)).toBeNull()
    }
    // this home's own kc- box with no row keeps its buttons and no line
    expect(screen.getByTestId('sandbox-resume-sb-2')).toBeTruthy()
    expect(screen.getByTestId('sandbox-delete-sb-2')).toBeTruthy()
    expect(screen.queryByTestId('sandbox-marker-sb-2')).toBeNull()
    expect(managedEnvApi.sandboxList).toHaveBeenCalledTimes(1)
    expect(post).not.toHaveBeenCalled()
  })

  it('every sandbox has a crew row: says where they are', async () => {
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1')])
    mountForm()
    const line = await screen.findByTestId('sandbox-all-have-crews')
    expect(line.textContent).toBe(
      'Each of your sandboxes has a crew: manage it under Your crews. Stop, Resume and Idle timeout are on its row there, and Delete is in its menu.',
    )
    expect(screen.queryByTestId('sandbox-row-sb-1')).toBeNull()
    expect(screen.queryByTestId('sandbox-empty')).toBeNull()
    expect(screen.queryByTestId('sandbox-with-crew-pointer')).toBeNull()
  })

  it.each([
    [1, '1 sandbox has a crew: manage it under Your crews.'],
    [2, '2 sandboxes have a crew: manage them under Your crews.'],
  ] as const)('%s with a crew row gives one pointer line; the rest keep their buttons', async (n, line) => {
    const withRows = Array.from({ length: n }, (_, k) => box(`sb-c${k}`, 'RUNNING', `c${k}`))
    listAnswer = answer([...withRows, box('sb-2', 'STOPPED', '')])
    mountForm()
    expect((await screen.findByTestId('sandbox-with-crew-pointer')).textContent).toBe(line)
    expect(withCrewPointer(n)).toBe(line)
    for (const r of withRows) expect(screen.queryByTestId(`sandbox-row-${r.sandbox_id}`)).toBeNull()
    expect(screen.getByTestId('sandbox-resume-sb-2')).toBeTruthy()
    expect(screen.getByTestId('sandbox-delete-sb-2')).toBeTruthy()
  })
})

describe('the idle timeout on the crew row', () => {
  const idle = (name: string) => screen.queryByRole('menuitem', { name: `Idle timeout of ${name}` })

  /** The unit picker is a SimpleSelect: open its trigger and pick the option by its label. */
  async function pickUnit(sid: string, label: string) {
    const trigger = () => screen.getByTestId(`sandbox-idle-unit-${sid}`).querySelector('[role="combobox"]') as HTMLElement
    if (trigger().textContent === label) return
    const u = userEvent.setup()
    await u.click(trigger())
    await u.click(await screen.findByRole('option', { name: label }))
    await waitFor(() => expect(trigger().textContent).toBe(label))
  }

  const unitShown = (sid: string) =>
    screen.getByTestId(`sandbox-idle-unit-${sid}`).querySelector('[role="combobox"]')?.textContent

  it('shows the stored value on the row, and the item opens the dialog', async () => {
    crews = [crew('c1', 'run-a')]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1', { idle_timeout_minutes: 60 })])
    const u = userEvent.setup()
    await renderPanel()
    expect((await screen.findByTestId('sandbox-row-idle-c1')).textContent).toBe(
      "Stops after 60 minutes with no activity, where idle stop is on. An open but quiet connection doesn't count.",
    )
    await openMenu(u, 'run-a')
    await u.click(idle('run-a') as HTMLElement)
    expect(await screen.findByTestId('sandbox-idle-dialog-sb-1')).toBeTruthy()
  })

  it('RUNNING: the warning first, then Save and restart posts the minutes', async () => {
    crews = [crew('c1', 'run-a')]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1', { idle_timeout_minutes: 60 })])
    const post = vi.spyOn(managedEnvApi, 'sandboxIdleTimeout').mockResolvedValue({ ok: true, action: 'idle-timeout' })
    const u = userEvent.setup()
    await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')
    await openMenu(u, 'run-a')
    await u.click(idle('run-a') as HTMLElement)
    expect((await screen.findByTestId('sandbox-idle-effect-sb-1')).textContent).toBe(
      'Changing it restarts the sandbox now. Running work and chats stop.',
    )
    const save = screen.getByTestId('sandbox-idle-save-sb-1')
    expect(save.textContent).toBe('Save and restart')
    await u.clear(screen.getByTestId('sandbox-idle-minutes-sb-1'))
    await u.type(screen.getByTestId('sandbox-idle-minutes-sb-1'), '45')
    expect(post).not.toHaveBeenCalled()
    await u.click(save)
    await waitFor(() => expect(post).toHaveBeenCalledWith('sb-1', 45))
    expect(post).toHaveBeenCalledTimes(1)
  })

  it('STOPPED: takes effect at resume, plain Save; presets fill the value', async () => {
    crews = [crew('c1', 'stop-b')]
    listAnswer = answer([box('sb-1', 'STOPPED', 'c1', { idle_timeout_minutes: 60 })])
    const post = vi.spyOn(managedEnvApi, 'sandboxIdleTimeout').mockResolvedValue({ ok: true, action: 'idle-timeout' })
    const u = userEvent.setup()
    await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')
    await openMenu(u, 'stop-b')
    await u.click(idle('stop-b') as HTMLElement)
    expect((await screen.findByTestId('sandbox-idle-effect-sb-1')).textContent).toBe(
      'It takes effect when you resume the sandbox.',
    )
    await u.click(screen.getByTestId('sandbox-idle-preset-240'))
    const save = screen.getByTestId('sandbox-idle-save-sb-1')
    expect(save.textContent).toBe('Save')
    await u.click(save)
    await waitFor(() => expect(post).toHaveBeenCalledWith('sb-1', 240))
  })

  it("the row's Idle timeout button opens the dialog, and Save sends one change", async () => {
    crews = [crew('c1', 'stop-b')]
    listAnswer = answer([box('sb-1', 'STOPPED', 'c1', { idle_timeout_minutes: 60 })])
    const post = vi.spyOn(managedEnvApi, 'sandboxIdleTimeout').mockResolvedValue({ ok: true, action: 'idle-timeout' })
    const u = userEvent.setup()
    await renderPanel()
    await u.click(await screen.findByTestId('sandbox-row-idle-btn-c1'))
    await screen.findByTestId('sandbox-idle-dialog-sb-1')
    await u.click(screen.getByTestId('sandbox-idle-preset-240'))
    await u.click(screen.getByTestId('sandbox-idle-save-sb-1'))
    await waitFor(() => expect(post).toHaveBeenCalledWith('sb-1', 240))
    expect(post).toHaveBeenCalledTimes(1)
  })

  it('the Idle timeout button greys with the menu item while idle_off', async () => {
    crews = [crew('c1', 'run-a')]
    listAnswer = { ...answer([box('sb-1', 'RUNNING', 'c1')]), idle_off: true }
    await renderPanel()
    const b = (await screen.findByTestId('sandbox-row-idle-btn-c1')) as HTMLButtonElement
    expect(b.disabled).toBe(true)
    expect((screen.getByTestId('sandbox-row-stop-btn-c1') as HTMLButtonElement).disabled).toBe(false)
  })

  it('a number in minutes, hours or days, 15 minutes to 7 days, saved as one change', async () => {
    crews = [crew('c1', 'stop-b')]
    listAnswer = answer([box('sb-1', 'STOPPED', 'c1', { idle_timeout_minutes: 60 })])
    const post = vi.spyOn(managedEnvApi, 'sandboxIdleTimeout').mockResolvedValue({ ok: true, action: 'idle-timeout' })
    const action = vi.spyOn(managedEnvApi, 'sandboxAction')
    const u = userEvent.setup()
    await renderPanel()
    await u.click(await screen.findByTestId('sandbox-row-idle-btn-c1'))
    const input = await screen.findByTestId('sandbox-idle-minutes-sb-1')
    const save = () => screen.getByTestId('sandbox-idle-save-sb-1') as HTMLButtonElement
    const refused = () => screen.queryByTestId('sandbox-idle-refused-sb-1')
    expect(screen.getByTestId('sandbox-idle-hint-sb-1').textContent).toBe('Any whole number from 15 minutes to 7 days.')
    const cases: Array<[string, string, boolean]> = [
      ['14', 'minutes', false], ['15', 'minutes', true], ['169', 'hours', false], ['168', 'hours', true],
      ['8', 'days', false], ['0', 'days', false], ['1.5', 'hours', false], ['7', 'days', true],
    ]
    for (const [n, un, ok] of cases) {
      await u.clear(input)
      await u.type(input, n)
      await pickUnit('sb-1', un)
      expect(save().disabled, `${n} ${un}`).toBe(!ok)
      expect(Boolean(refused()), `${n} ${un}`).toBe(!ok)
    }
    // 7 days is in the field: Save sends its minutes, once, and nothing else
    await u.click(save())
    await waitFor(() => expect(post).toHaveBeenCalledWith('sb-1', 10080))
    expect(post).toHaveBeenCalledTimes(1)
    expect(action).not.toHaveBeenCalled()
  })

  it('a quick pick fills the number and its unit', async () => {
    crews = [crew('c1', 'stop-b')]
    listAnswer = answer([box('sb-1', 'STOPPED', 'c1', { idle_timeout_minutes: 60 })])
    const u = userEvent.setup()
    await renderPanel()
    await u.click(await screen.findByTestId('sandbox-row-idle-btn-c1'))
    for (const [preset, n, un] of [[60, '1', 'hours'], [240, '4', 'hours'], [1440, '1', 'days'], [10080, '7', 'days']] as const) {
      await u.click(screen.getByTestId(`sandbox-idle-preset-${preset}`))
      expect((screen.getByTestId('sandbox-idle-minutes-sb-1') as HTMLInputElement).value).toBe(n)
      await waitFor(() => expect(unitShown('sb-1')).toBe(un))
    }
  })

  it('refuses 14, 10081 and 60.5 in the dialog, and any other state has no Save', async () => {
    crews = [crew('c1', 'stop-b'), crew('c2', 'pend-c')]
    listAnswer = answer([
      box('sb-1', 'STOPPED', 'c1', { idle_timeout_minutes: 60 }),
      box('sb-2', 'PENDING', 'c2', { idle_timeout_minutes: 60 }),
    ])
    const post = vi.spyOn(managedEnvApi, 'sandboxIdleTimeout')
    const u = userEvent.setup()
    await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')
    await openMenu(u, 'stop-b')
    await u.click(idle('stop-b') as HTMLElement)
    const input = await screen.findByTestId('sandbox-idle-minutes-sb-1')
    for (const bad of ['14', '10081', '60.5']) {
      await u.clear(input)
      await u.type(input, bad)
      expect((screen.getByTestId('sandbox-idle-save-sb-1') as HTMLButtonElement).disabled, bad).toBe(true)
      expect(screen.getByText('The idle timeout is a whole number of minutes, 15 minutes to 7 days.')).toBeTruthy()
    }
    await u.keyboard('{Escape}')
    await openMenu(u, 'pend-c')
    await u.click(idle('pend-c') as HTMLElement)
    expect((await screen.findByTestId('sandbox-idle-effect-sb-2')).textContent).toBe(
      'The sandbox is busy (starting, stopping or restarting). Try again in a moment.',
    )
    expect((screen.getByTestId('sandbox-idle-save-sb-2') as HTMLButtonElement).disabled).toBe(true)
    expect(post).not.toHaveBeenCalled()
  })

  it("after the switch's answer: the off text on the row and the item greyed", async () => {
    crews = [crew('c1', 'run-a')]
    listAnswer = { ...answer([box('sb-1', 'RUNNING', 'c1', { idle_timeout_minutes: 60 })]), idle_off: true }
    const u = userEvent.setup()
    await renderPanel()
    expect((await screen.findByTestId('sandbox-row-idle-c1')).textContent).toBe(
      "Idle stop is off for your account, so this sandbox doesn't stop on its own. Stop it when you're done.",
    )
    await openMenu(u, 'run-a')
    expect(idle('run-a')?.getAttribute('aria-disabled')).toBe('true')
  })

  it('while another action runs: no Idle timeout item', async () => {
    crews = [crew('c1', 'run-a'), crew('c2', 'stop-b')]
    listAnswer = answer([
      box('sb-1', 'RUNNING', 'c1', { idle_timeout_minutes: 60, action: busy }),
      box('sb-2', 'STOPPED', 'c2', { idle_timeout_minutes: 60, action: { ...busy, action: 'resume' } }),
    ])
    const u = userEvent.setup()
    await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')
    for (const name of ['run-a', 'stop-b']) {
      await openMenu(u, name)
      expect(idle(name), name).toBeNull()
      await u.keyboard('{Escape}')
    }
  })

  it('an action that starts while the dialog is open takes Save away', async () => {
    crews = [crew('c1', 'run-a')]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1', { idle_timeout_minutes: 60 })])
    const post = vi.spyOn(managedEnvApi, 'sandboxIdleTimeout')
    const u = userEvent.setup()
    const { queryClient } = await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')
    await openMenu(u, 'run-a')
    await u.click(idle('run-a') as HTMLElement)
    expect((await screen.findByTestId('sandbox-idle-save-sb-1') as HTMLButtonElement).disabled).toBe(false)
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1', { idle_timeout_minutes: 60, action: busy })])
    await queryClient.invalidateQueries({ queryKey: SANDBOX_LIST_QUERY_KEY })
    await waitFor(() =>
      expect((screen.getByTestId('sandbox-idle-save-sb-1') as HTMLButtonElement).disabled).toBe(true))
    expect(screen.getByTestId('sandbox-idle-effect-sb-1').textContent).toBe(
      'The sandbox is busy (starting, stopping or restarting). Try again in a moment.',
    )
    expect(post).not.toHaveBeenCalled()
  })

  it("shows the action's note: the restart hasn't begun yet", async () => {
    crews = [crew('c1', 'run-a')]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1', {
      idle_timeout_minutes: 45,
      action: { action: 'idle-timeout', running: false, error: '', at: 1, note: "Saved. The restart hasn't begun yet; chats stop when it does." },
    })])
    await renderPanel()
    expect((await screen.findByTestId('sandbox-row-note-c1')).textContent).toBe(
      "Saved. The restart hasn't begun yet; chats stop when it does.",
    )
  })
})

describe('the connect: no list wait after the sign-in, the re-read while connecting, the phase', () => {
  const failed = (id: string, error = 'ssh exited 255: mint-and-attach: Sign in to connect.') =>
    ({ instance_id: id, state: 'error', error }) as InstanceView['status']
  const connecting = (id: string) => ({ instance_id: id, state: 'connecting' }) as InstanceView['status']
  const connected = (id: string) => ({ instance_id: id, state: 'connected' }) as InstanceView['status']
  /** The list answer of a page booted signed out: the rows, no state, the list's error. */
  const signedOutList = (rows: [string, string][]): SandboxList => ({
    sandboxes: rows.map(([sid, iid]) => box(sid, '', iid)),
    error: 'not signed in',
    row_lifecycle: true,
  })

  beforeEach(() => {
    vi.spyOn(managedEnvApi, 'sandboxConnectPhase').mockResolvedValue({ phase: '' })
  })

  it('a page booted signed out: once signed in, one connect per row in error, with no list read first', async () => {
    signinAnswer = { ...signinAnswer, state: 'signin_required' }
    crews = [crew('c1', 'run-a', { status: failed('c1') }), crew('c2', 'run-b', { status: failed('c2') })]
    listAnswer = signedOutList([['sb-c1', 'c1'], ['sb-c2', 'c2']])
    const { queryClient } = await renderPanel()
    await waitFor(() => expect(managedEnvApi.sandboxList).toHaveBeenCalled())
    await waitFor(() => expect(managedEnvApi.sandboxRowLifecycle).toHaveBeenCalled())
    expect(api.connectInstance).not.toHaveBeenCalled()
    const listReads = () => vi.mocked(managedEnvApi.sandboxList).mock.calls.length
    const readsAtConnect: number[] = []
    vi.mocked(api.connectInstance).mockImplementation(async () => {
      readsAtConnect.push(listReads())
      return { instance_id: 'x', state: 'connected' } as never
    })
    const before = listReads()
    signinAnswer = { ...signinAnswer, state: 'signed_in' }
    await queryClient.invalidateQueries({ queryKey: SANDBOX_ROW_LIFECYCLE_QUERY_KEY })
    await waitFor(() => expect(vi.mocked(api.connectInstance).mock.calls.length).toBe(2))
    expect(vi.mocked(api.connectInstance).mock.calls.map(c => c[0]).sort()).toEqual(['c1', 'c2'])
    expect(readsAtConnect).toEqual([before, before])
  })

  it('a box in fact stopped: the one connect gets the stopped line, and no second one follows', async () => {
    signinAnswer = { ...signinAnswer, state: 'signin_required' }
    crews = [crew('c1', 'run-a', { status: failed('c1') })]
    listAnswer = signedOutList([['sb-c1', 'c1']])
    const { queryClient } = await renderPanel()
    await waitFor(() => expect(managedEnvApi.sandboxRowLifecycle).toHaveBeenCalled())
    signinAnswer = { ...signinAnswer, state: 'signed_in' }
    await queryClient.invalidateQueries({ queryKey: SANDBOX_ROW_LIFECYCLE_QUERY_KEY })
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('c1'))
    crews = [crew('c1', 'run-a', { status: failed('c1', 'ssh exited 255: mint-and-attach: Stopped. Resume it, then Connect.') })]
    const reads = vi.mocked(api.listInstances).mock.calls.length
    await queryClient.invalidateQueries({ queryKey: ['instances'] })
    await waitFor(() => expect(vi.mocked(api.listInstances).mock.calls.length).toBeGreaterThan(reads))
    const routeReads = vi.mocked(managedEnvApi.sandboxRowLifecycle).mock.calls.length
    await queryClient.invalidateQueries({ queryKey: SANDBOX_ROW_LIFECYCLE_QUERY_KEY })
    await waitFor(() =>
      expect(vi.mocked(managedEnvApi.sandboxRowLifecycle).mock.calls.length).toBeGreaterThan(routeReads))
    expect(vi.mocked(api.connectInstance).mock.calls).toEqual([['c1']])
  })

  function invalidated(qc: QueryClient) {
    const spy = vi.spyOn(qc, 'invalidateQueries')
    return (key: string) =>
      spy.mock.calls.filter(c => JSON.stringify((c[0] as { queryKey?: unknown } | undefined)?.queryKey) === key).length
  }

  it('while rows connect: one page timer re-reads only the crews query every 2 s, and stops on connected', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      crews = [crew('c1', 'run-a', { status: connecting('c1') }), crew('c2', 'run-b', { status: connecting('c2') })]
      listAnswer = answer([box('sb-c1', 'RUNNING', 'c1'), box('sb-c2', 'RUNNING', 'c2')])
      const { queryClient } = await renderPanel()
      await screen.findByTestId('sandbox-row-state-c2')
      const count = invalidated(queryClient)
      await vi.advanceTimersByTimeAsync(10_000)
      // two rows connecting, one timer: 5 re-reads in 10 s (6 if the clock ran on meanwhile)
      expect(count(CREWS_KEY)).toBeGreaterThanOrEqual(5)
      expect(count(CREWS_KEY)).toBeLessThanOrEqual(6)
      expect(count(LIST_KEY)).toBe(0)
      // the core reads connected: at the next re-read the rows leave connecting, and it stops
      crews = [crew('c1', 'run-a', { status: connected('c1') }), crew('c2', 'run-b', { status: connected('c2') })]
      const reads = vi.mocked(api.listInstances).mock.calls.length
      await vi.advanceTimersByTimeAsync(2_000)
      await waitFor(() => expect(vi.mocked(api.listInstances).mock.calls.length).toBeGreaterThan(reads))
      await vi.advanceTimersByTimeAsync(2_000)
      const settled = count(CREWS_KEY)
      await vi.advanceTimersByTimeAsync(20_000)
      expect(count(CREWS_KEY)).toBe(settled)
      expect(count(LIST_KEY)).toBe(0)
      cleanup()
    } finally {
      vi.useRealTimers()
    }
  })

  it('a row still connecting after 60 s: the re-reads stop at the cap', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      crews = [crew('c1', 'run-a', { status: connecting('c1') })]
      listAnswer = answer([box('sb-c1', 'RUNNING', 'c1')])
      const { queryClient } = await renderPanel()
      await screen.findByTestId('sandbox-row-state-c1')
      const count = invalidated(queryClient)
      await vi.advanceTimersByTimeAsync(120_000)
      const atCap = count(CREWS_KEY)
      expect(atCap).toBeGreaterThanOrEqual(27)
      expect(atCap).toBeLessThanOrEqual(30)
      await vi.advanceTimersByTimeAsync(30_000)
      expect(count(CREWS_KEY)).toBe(atCap)
      expect(count(LIST_KEY)).toBe(0)
      cleanup()
    } finally {
      vi.useRealTimers()
    }
  })

  it("shows the row's phase, read by its own host's id with no list item, and drops it once connected", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      crews = [crew('c1', 'run-a', { status: connecting('c1') })] // ssh_host crew-sbx-sb-c1
      listAnswer = signedOutList([]) // no list item for this row at all
      const phases: SandboxConnectPhase['phase'][] = ['starting', 'tunnel', 'signing_in']
      let n = 0
      const read = vi
        .spyOn(managedEnvApi, 'sandboxConnectPhase')
        .mockImplementation(async () => ({ phase: phases[Math.min(n++, phases.length - 1)] }))
      await renderPanel()
      const line = () => screen.queryByTestId('sandbox-row-phase-c1')?.textContent
      await waitFor(() => expect(line()).toBe('Starting the session…'))
      await vi.advanceTimersByTimeAsync(2_000)
      await waitFor(() => expect(line()).toBe('Opening the tunnel…'))
      await vi.advanceTimersByTimeAsync(2_000)
      await waitFor(() => expect(line()).toBe('Signing in to the crew…'))
      expect(read.mock.calls.every(c => c[0] === 'sb-c1')).toBe(true)
      crews = [crew('c1', 'run-a', { status: connected('c1') })]
      await vi.advanceTimersByTimeAsync(2_000)
      await waitFor(() => expect(line()).toBeUndefined())
      cleanup()
    } finally {
      vi.useRealTimers()
    }
  })

  it('lane off: no phase read and no re-read while a row connects', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      crews = [crew('c1', 'run-a', { status: connecting('c1') })]
      listAnswer = answer([box('sb-c1', 'RUNNING', 'c1')])
      laneOff()
      const { queryClient } = await renderPanel()
      await waitFor(() => expect(managedEnvApi.sandboxRowLifecycle).toHaveBeenCalled())
      const count = invalidated(queryClient)
      await vi.advanceTimersByTimeAsync(10_000)
      expect(count(CREWS_KEY)).toBe(0)
      expect(managedEnvApi.sandboxConnectPhase).not.toHaveBeenCalled()
      cleanup()
    } finally {
      vi.useRealTimers()
    }
  })

  it.each([
    ['crew-sbx-sb-1', 'sb-1'],
    ['crew-sbx-a.b_c-1', 'a.b_c-1'],
    ['crew-sbx-', ''],
    ['crew-sbx-../x', ''],
    ['crew-sbx--x', ''],
    ['crew-sess-abc', ''],
    ['', ''],
  ])('the sandbox id of host %j is %j (the engine rule)', (host, sid) => {
    expect(sandboxIdFromHost(host)).toBe(sid)
  })
})

describe('rows from the List: Delete by deletable, Start Crew', () => {
  const failedConnect = (id: string) =>
    ({ instance_id: id, state: 'error', error: 'ssh exited 255: health check failed' }) as InstanceView['status']
  // an answer the List decided (crews_from_list on)
  const fromList = (sandboxes: SandboxItem[]): SandboxList => ({ ...answer(sandboxes), crews_from_list: true })

  it('an accepted box (deletable false) offers Stop, Resume and Idle, never Delete, on its row and in the form', async () => {
    crews = [crew('c1', 'kc-0a1b2c')]
    listAnswer = answer([
      box('sb-1', 'RUNNING', 'c1', { deletable: false }), // a row the List made
      box('sb-2', 'STOPPED', '', { deletable: false }), // accepted, its row removed by the user
    ])
    const u = userEvent.setup()
    await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')
    await openMenu(u, 'kc-0a1b2c')
    expect(items('kc-0a1b2c').stop).toBeTruthy()
    expect(items('kc-0a1b2c').delete).toBeNull()
    expect(screen.getByTestId('sandbox-row-stop-btn-c1')).toBeTruthy()
    cleanup()
    mountForm()
    expect(await screen.findByTestId('sandbox-resume-sb-2')).toBeTruthy()
    expect(screen.queryByTestId('sandbox-delete-sb-2')).toBeNull() // no Delete button at all
  })

  it('an answer with rows_added re-reads the crews list', async () => {
    crews = [crew('c1', 'run-a')]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1')])
    const { queryClient } = await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')
    await queryClient.invalidateQueries({ queryKey: SANDBOX_LIST_QUERY_KEY })
    await waitFor(() => expect(managedEnvApi.sandboxList).toHaveBeenCalledTimes(2))
    const n = vi.mocked(api.listInstances).mock.calls.length
    listAnswer = { ...answer([box('sb-1', 'RUNNING', 'c1')]), rows_added: true }
    await queryClient.invalidateQueries({ queryKey: SANDBOX_LIST_QUERY_KEY })
    await waitFor(() => expect(vi.mocked(api.listInstances).mock.calls.length).toBeGreaterThan(n))
  })

  it('a RUNNING row whose Connect failed gets the line and Start Crew; a start makes one Connect', async () => {
    crews = [crew('c1', 'kc-0a1b2c', { status: failedConnect('c1') })]
    listAnswer = fromList([box('sb-1', 'RUNNING', 'c1', { deletable: false })])
    const start = vi.spyOn(managedEnvApi, 'sandboxStartCrew').mockResolvedValue({ started: true, line: '' })
    const u = userEvent.setup()
    await renderPanel()
    const down = await screen.findByTestId('sandbox-row-gateway-down-c1')
    expect(down.textContent).toContain("Crew's gateway isn't running on this sandbox.")
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalled()) // the one connect after sign-in
    const before = vi.mocked(api.connectInstance).mock.calls.length
    await u.click(screen.getByTestId('sandbox-row-start-crew-c1'))
    await waitFor(() => expect(start).toHaveBeenCalledWith('sb-1'))
    await waitFor(() => expect(vi.mocked(api.connectInstance).mock.calls.length).toBe(before + 1))
    expect(start).toHaveBeenCalledTimes(1)
  })

  it('a failed start shows its plain line and makes no Connect', async () => {
    crews = [crew('c1', 'kc-0a1b2c', { status: failedConnect('c1') })]
    listAnswer = fromList([box('sb-1', 'RUNNING', 'c1', { deletable: false })])
    const line = 'Crew on this sandbox needs a fresh token. Press Connect, then Start Crew.'
    vi.spyOn(managedEnvApi, 'sandboxStartCrew').mockResolvedValue({ started: false, line })
    const u = userEvent.setup()
    await renderPanel()
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalled())
    const before = vi.mocked(api.connectInstance).mock.calls.length
    await u.click(await screen.findByTestId('sandbox-row-start-crew-c1'))
    expect((await screen.findByTestId('sandbox-row-start-crew-line-c1')).textContent).toBe(line)
    expect(vi.mocked(api.connectInstance).mock.calls.length).toBe(before)
  })

  it("right after RUNNING, with no failed Connect yet, no gateway line; and none on another client's box", async () => {
    crews = [crew('c1', 'kc-0a1b2c'), crew('c2', 'other', { status: failedConnect('c2') })]
    listAnswer = fromList([
      box('sb-1', 'RUNNING', 'c1', { deletable: false }),
      box('sb-2', 'RUNNING', 'c2', { own: false }),
    ])
    await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')
    expect(screen.queryByTestId('sandbox-row-gateway-down-c1')).toBeNull()
    expect(screen.queryByTestId('sandbox-row-gateway-down-c2')).toBeNull()
  })

  it('with crews_from_list absent from the answer, no Start Crew on a failed RUNNING row', async () => {
    crews = [crew('c1', 'kc-0a1b2c', { status: failedConnect('c1') })]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1')])
    const start = vi.spyOn(managedEnvApi, 'sandboxStartCrew')
    await renderPanel()
    expect(await screen.findByTestId('sandbox-row-unreachable-c1')).toBeTruthy() // the plain line stays
    expect(screen.queryByTestId('sandbox-row-gateway-down-c1')).toBeNull()
    expect(screen.queryByTestId('sandbox-row-start-crew-c1')).toBeNull()
    expect(start).not.toHaveBeenCalled()
  })
})

describe('Set up Crew and Resume on a box with no record', () => {
  const noRecord = (sid: string, status: string, extra: Partial<SandboxItem> = {}) =>
    box(sid, status, '', { own: false, crew_marker: false, deletable: false, ...extra })
  const decided = (sandboxes: SandboxItem[]): SandboxList => ({ ...answer(sandboxes), crews_from_list: true })
  const notFound = "Crew can't be set up on a sandbox it didn't create yet. To use Crew, create a new sandbox under Set up a new one."

  it('STOPPED: Resume and Set up Crew; never Stop or Delete', async () => {
    listAnswer = decided([noRecord('sb-3', 'STOPPED')])
    mountForm()
    expect((await screen.findByTestId('sandbox-set-up-sb-3')).textContent).toBe('Set up Crew')
    expect(screen.getByTestId('sandbox-resume-sb-3')).toBeTruthy()
    expect(screen.queryByTestId('sandbox-stop-sb-3')).toBeNull()
    expect(screen.queryByTestId('sandbox-delete-sb-3')).toBeNull()
  })

  it('RUNNING: Set up Crew only', async () => {
    listAnswer = decided([noRecord('sb-4', 'RUNNING')])
    mountForm()
    expect(await screen.findByTestId('sandbox-set-up-sb-4')).toBeTruthy()
    expect(screen.queryByTestId('sandbox-resume-sb-4')).toBeNull()
    expect(screen.queryByTestId('sandbox-stop-sb-4')).toBeNull()
  })

  it('Set up Crew posts once and shows the not-found line', async () => {
    listAnswer = decided([noRecord('sb-4', 'RUNNING')])
    const spy = vi.spyOn(managedEnvApi, 'sandboxSetUpCrew').mockResolvedValue({ result: 'not_found', line: notFound })
    const u = userEvent.setup()
    mountForm()
    await u.click(await screen.findByTestId('sandbox-set-up-sb-4'))
    expect((await screen.findByTestId('sandbox-setup-line-sb-4')).textContent).toBe(notFound)
    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy).toHaveBeenCalledWith('sb-4')
  })

  it('found: the plain found line', async () => {
    listAnswer = decided([noRecord('sb-4', 'RUNNING')])
    const found = "Crew is on this sandbox. It's under Your crews now."
    vi.spyOn(managedEnvApi, 'sandboxSetUpCrew').mockResolvedValue({ result: 'found', line: found })
    const u = userEvent.setup()
    mountForm()
    await u.click(await screen.findByTestId('sandbox-set-up-sb-4'))
    expect((await screen.findByTestId('sandbox-setup-found')).textContent).toBe(found)
  })

  it("Resume on a STOPPED box is the resume action; the read after it comes back as the list's line", async () => {
    listAnswer = decided([noRecord('sb-3', 'STOPPED')])
    const post = vi.spyOn(managedEnvApi, 'sandboxAction').mockResolvedValue({ ok: true, action: 'resume' } as never)
    const u = userEvent.setup()
    mountForm()
    await u.click(await screen.findByTestId('sandbox-resume-sb-3'))
    await waitFor(() => expect(post).toHaveBeenCalledWith('sb-3', 'resume'))
    cleanup()
    listAnswer = decided([noRecord('sb-3', 'RUNNING', { setup_line: notFound })])
    mountForm()
    expect((await screen.findByTestId('sandbox-setup-line-sb-3')).textContent).toBe(notFound)
  })

  it('with crews_from_list absent: the plain line, no buttons, no setup line', async () => {
    listAnswer = answer([noRecord('sb-4', 'RUNNING', { setup_line: notFound }), noRecord('sb-3', 'STOPPED')])
    mountForm()
    expect(await screen.findByTestId('sandbox-not-set-up-sb-4')).toBeTruthy()
    for (const id of ['sandbox-set-up-sb-4', 'sandbox-set-up-sb-3', 'sandbox-resume-sb-3', 'sandbox-setup-line-sb-4']) {
      expect(screen.queryByTestId(id)).toBeNull()
    }
  })

  it('a box with the Crew marker, or one in an action, gets no Set up Crew', async () => {
    listAnswer = decided([
      noRecord('sb-k', 'RUNNING', { crew_marker: true }),
      noRecord('sb-b', 'STOPPED', { action: { action: 'resume', running: true, error: '', at: 1 } }),
    ])
    mountForm()
    expect(await screen.findByTestId('sandbox-marker-sb-k')).toBeTruthy()
    expect(screen.queryByTestId('sandbox-set-up-sb-k')).toBeNull()
    expect(screen.queryByTestId('sandbox-set-up-sb-b')).toBeNull()
  })
})

describe('Rename on a crew row', () => {
  const decided = (sandboxes: SandboxItem[]): SandboxList => ({ ...answer(sandboxes), crews_from_list: true })
  const renameItem = (name: string) => screen.queryByRole('menuitem', { name: `Rename ${name}` })

  it('the menu offers Rename… only while the List decides, on an own box', async () => {
    crews = [crew('c1', 'crew-sandbox-01')]
    listAnswer = answer([box('sb-1', 'RUNNING', 'c1')])
    const u = userEvent.setup()
    await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')
    await openMenu(u, 'crew-sandbox-01')
    expect(renameItem('crew-sandbox-01')).toBeNull()
    cleanup()
    listAnswer = decided([box('sb-1', 'RUNNING', 'c1')])
    await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')
    await openMenu(u, 'crew-sandbox-01')
    expect(renameItem('crew-sandbox-01')).toBeTruthy()
  })

  it('a failed rename shows in the dialog, which stays open with the old name; nothing on the row', async () => {
    crews = [crew('c1', 'crew-sandbox-01')]
    listAnswer = decided([box('sb-1', 'STOPPED', 'c1')])
    const failed = "Rename failed: renaming isn't available yet."
    const spy = vi.spyOn(managedEnvApi, 'sandboxRename').mockResolvedValue({ renamed: 'no', line: failed })
    const u = userEvent.setup()
    await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')
    await openMenu(u, 'crew-sandbox-01')
    await u.click(renameItem('crew-sandbox-01') as HTMLElement)
    const input = await screen.findByTestId('sandbox-rename-input-sb-1')
    await u.clear(input)
    await u.type(input, 'My Crew')
    expect((screen.getByTestId('sandbox-rename-save-sb-1') as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByTestId('sandbox-rename-refused-sb-1')).toBeTruthy()
    await u.clear(input)
    await u.type(input, 'my-crew')
    await u.click(screen.getByTestId('sandbox-rename-save-sb-1'))
    const shown = await screen.findByTestId('sandbox-rename-error-sb-1')
    expect(shown.textContent).toContain(failed)
    expect(shown.querySelector('svg')).toBeTruthy() // the row error style: the warning icon
    // the dialog stays open, the old name back in the field
    expect(screen.getByTestId('sandbox-rename-dialog-sb-1')).toBeTruthy()
    expect((screen.getByTestId('sandbox-rename-input-sb-1') as HTMLInputElement).value).toBe('crew-sandbox-01')
    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy).toHaveBeenCalledWith('sb-1', 'my-crew')
    // nothing lasting on the row
    expect(screen.getByTestId('sandbox-row-detail-c1').textContent).not.toContain(failed)
    // Cancel closes it and clears the failure; reopened, the dialog is clean
    await u.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByTestId('sandbox-rename-dialog-sb-1')).toBeNull())
    expect(screen.getByTestId('sandbox-row-detail-c1').textContent).not.toContain(failed)
    await openMenu(u, 'crew-sandbox-01')
    await u.click(renameItem('crew-sandbox-01') as HTMLElement)
    await screen.findByTestId('sandbox-rename-dialog-sb-1')
    expect(screen.queryByTestId('sandbox-rename-error-sb-1')).toBeNull()
  })

  it('a failed answer that lands after Cancel sets no failure; the next open is clean', async () => {
    crews = [crew('c1', 'crew-sandbox-01')]
    listAnswer = decided([box('sb-1', 'RUNNING', 'c1')])
    let answerNow: (a: { renamed: 'no'; line: string }) => void = () => undefined
    vi.spyOn(managedEnvApi, 'sandboxRename').mockImplementation(
      () => new Promise(resolve => { answerNow = resolve }),
    )
    const u = userEvent.setup()
    await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')
    await openMenu(u, 'crew-sandbox-01')
    await u.click(renameItem('crew-sandbox-01') as HTMLElement)
    const input = await screen.findByTestId('sandbox-rename-input-sb-1')
    await u.clear(input)
    await u.type(input, 'my-crew')
    await u.click(screen.getByTestId('sandbox-rename-save-sb-1')) // saving
    await u.click(screen.getByRole('button', { name: 'Cancel' })) // Cancel stays enabled
    await waitFor(() => expect(screen.queryByTestId('sandbox-rename-dialog-sb-1')).toBeNull())
    answerNow({ renamed: 'no', line: "Rename failed: renaming isn't available yet." })
    await new Promise(r => setTimeout(r, 0))
    await openMenu(u, 'crew-sandbox-01')
    await u.click(renameItem('crew-sandbox-01') as HTMLElement)
    await screen.findByTestId('sandbox-rename-dialog-sb-1')
    expect(screen.queryByTestId('sandbox-rename-error-sb-1')).toBeNull()
  })

  it('a refused post shows in the dialog too, not on the row', async () => {
    crews = [crew('c1', 'crew-sandbox-01')]
    listAnswer = decided([box('sb-1', 'RUNNING', 'c1')])
    vi.spyOn(managedEnvApi, 'sandboxRename').mockRejectedValue(new Error('A rename is already running here.'))
    const u = userEvent.setup()
    await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')
    await openMenu(u, 'crew-sandbox-01')
    await u.click(renameItem('crew-sandbox-01') as HTMLElement)
    const input = await screen.findByTestId('sandbox-rename-input-sb-1')
    await u.clear(input)
    await u.type(input, 'my-crew')
    await u.click(screen.getByTestId('sandbox-rename-save-sb-1'))
    expect((await screen.findByTestId('sandbox-rename-error-sb-1')).textContent).toContain('A rename is already running here.')
    expect(screen.getByTestId('sandbox-rename-dialog-sb-1')).toBeTruthy()
    expect((screen.getByTestId('sandbox-rename-input-sb-1') as HTMLInputElement).value).toBe('crew-sandbox-01')
    expect(screen.getByTestId('sandbox-row-detail-c1').textContent).not.toContain('already running')
  })

  it('the same name is allowed: it shares the row name with the other dashboards', async () => {
    crews = [crew('c1', 'crew-sandbox-01')]
    listAnswer = decided([box('sb-1', 'RUNNING', 'c1')])
    const spy = vi.spyOn(managedEnvApi, 'sandboxRename').mockResolvedValue({ renamed: 'everywhere', name: 'crew-sandbox-01', line: '' })
    const u = userEvent.setup()
    await renderPanel()
    await screen.findByTestId('sandbox-row-state-c1')
    await openMenu(u, 'crew-sandbox-01')
    await u.click(renameItem('crew-sandbox-01') as HTMLElement)
    await u.click(await screen.findByTestId('sandbox-rename-save-sb-1'))
    await waitFor(() => expect(spy).toHaveBeenCalledWith('sb-1', 'crew-sandbox-01'))
    // a rename with no line closes the dialog
    await waitFor(() => expect(screen.queryByTestId('sandbox-rename-dialog-sb-1')).toBeNull())
  })
})
