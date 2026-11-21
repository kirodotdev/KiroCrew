/**
 * The managed-environment form: the sign-in status line (Crew's own sign-in, from the
 * row-lifecycle route), the local agent's line, Create gating, the buttons per status, Delete
 * asking first, route errors shown, and the one Create form with its defaults and checks.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const confirmAnswer = { value: true }
const confirmSpy = vi.fn(async () => confirmAnswer.value)
vi.mock('../ConfirmDialog', () => ({
  useConfirm: () => ({ confirm: confirmSpy, confirmDialog: null }),
}))

import SandboxLaunchForm, {
  SANDBOX_PROVISIONER_KIND,
  SANDBOX_ROW_LIFECYCLE_QUERY_KEY,
  SANDBOX_SIGNED_IN_USER_REFRESH_MS,
  sandboxActionsFor,
  sandboxCreateOptions,
  sandboxNameProblem,
} from './SandboxLaunchForm'
import { CREATE_TEXT } from './text'
import { KIRO_SIGN_IN_PATH } from '../../pages/developer/kiroSignInLink'
import { managedEnvApi } from '../../api/managedEnvApi'
import type { SandboxItem, SandboxRowLifecycle, SandboxSignedInUser, SandboxSignIn } from '../../api/managedEnvApi'

const SIGNED_IN = 'Signed in to Kiro.'
const REQUIRED = 'Sign in under Settings → Kiro account with your managed-environment start URL.'

/** The row-lifecycle route's answer, as the lane's routes send it. */
function routeAnswer(state: SandboxSignIn['state'], localAgent: SandboxSignIn['local_agent'] = 'on'): SandboxRowLifecycle {
  const message = state === 'signed_in' ? SIGNED_IN : REQUIRED
  return { row_lifecycle: true, sign_in: { state, message, local_agent: localAgent } }
}
const signedIn = routeAnswer('signed_in')
const idle = routeAnswer('signin_required')

/** An environment with no crew row (`instance_id` empty), which is what the form lists. */
function item(status: string, extra: Partial<SandboxItem> = {}): SandboxItem {
  return {
    sandbox_id: 'sb-1', status, display_name: 'kc-abc123', created_at: 1, instance_id: '',
    name: 'box', ssh_host: 'crew-sbx-sb-1', action: null, ...extra,
  }
}

const EMAIL = 'test0@example.com'
const user: { value: SandboxSignedInUser } = { value: { email: EMAIL } }

const provisioner = { id: SANDBOX_PROVISIONER_KIND, kind: SANDBOX_PROVISIONER_KIND } as never

function mount(props: Partial<Parameters<typeof SandboxLaunchForm>[0]> = {}) {
  const launch = vi.fn()
  vi.spyOn(managedEnvApi, 'sandboxSignedInUser').mockImplementation(async () => user.value)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <SandboxLaunchForm
        provisioner={provisioner}
        launch={launch}
        launching={false}
        disabled={false}
        activeJob={null}
        {...props}
      />
    </QueryClientProvider>,
  )
  return launch
}

/** The list answer the lane sends: `row_lifecycle` is on whenever the lane answers at all. */
function listing(...sandboxes: SandboxItem[]) {
  vi.spyOn(managedEnvApi, 'sandboxList').mockResolvedValue({ sandboxes, error: '', row_lifecycle: true })
}

/** The unit picker is a SimpleSelect: open its trigger and pick the option by its label. */
function unitTrigger(): HTMLElement {
  return within(screen.getByTestId('sandbox-create-idle-unit')).getByRole('combobox')
}

async function pickUnit(label: string) {
  fireEvent.click(unitTrigger())
  fireEvent.click(await screen.findByRole('option', { name: label }))
  await waitFor(() => expect(unitTrigger().textContent).toBe(label))
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  confirmSpy.mockClear()
  confirmAnswer.value = true
  user.value = { email: EMAIL }
})

describe('SandboxLaunchForm', () => {
  it('re-reads the list when a launch job starts and when it ends', async () => {
    vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...signedIn })
    vi.spyOn(managedEnvApi, 'sandboxSignedInUser').mockResolvedValue({ email: EMAIL })
    const spy = vi.spyOn(managedEnvApi, 'sandboxList').mockResolvedValue({ sandboxes: [], error: '', row_lifecycle: true })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const el = (job: unknown) => (
      <QueryClientProvider client={qc}>
        <SandboxLaunchForm
          provisioner={provisioner}
          launch={vi.fn()}
          launching={false}
          disabled={false}
          activeJob={job as never}
        />
      </QueryClientProvider>
    )
    const { rerender } = render(el(null))
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
    rerender(el({ id: 'j1', status: 'running' }))
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2))
    rerender(el({ id: 'j1', status: 'done' }))
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(3))
  })

  it('uses the kind the backend descriptor names', () => {
    expect(SANDBOX_PROVISIONER_KIND).toBe('kiro_managed_env')
  })

  it('not signed in: one line linking to the Kiro sign-in, and Create stays off', async () => {
    vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...idle })
    listing()
    const launch = mount()
    const line = await screen.findByTestId('sandbox-signin-line')
    expect(line.textContent).toBe(REQUIRED)
    expect(line.querySelector('a')?.getAttribute('href')).toBe(KIRO_SIGN_IN_PATH)
    expect(screen.queryByTestId('sandbox-local-agent-off')).toBeNull()
    const create = screen.getByTestId('sandbox-create') as HTMLButtonElement
    expect(create.disabled).toBe(true)
    fireEvent.click(create)
    expect(launch).not.toHaveBeenCalled()
  })

  it('signed in: the plain status line, with no link', async () => {
    vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...signedIn })
    listing()
    mount()
    const line = await screen.findByTestId('sandbox-signin-line')
    await waitFor(() => expect(line.textContent).toBe(`Signed in to Kiro as ${EMAIL}.`))
    expect(line.querySelector('a')).toBeNull()
    await waitFor(() => expect((screen.getByTestId('sandbox-create') as HTMLButtonElement).disabled).toBe(false))
  })

  it.each([
    [{ user_id_tail: 'a1b2c3d4' }, 'Signed in to Kiro (account …a1b2c3d4).'],
    [{ state: 'unknown' }, "Signed in to Kiro. The account name couldn't be read."],
  ] as const)('signed in, named by what the server read: %o', async (answer, text) => {
    user.value = answer as SandboxSignedInUser
    vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...signedIn })
    listing()
    mount()
    const line = await screen.findByTestId('sandbox-signin-line')
    await waitFor(() => expect(line.textContent).toBe(text))
  })

  it('an account switch that stays signed in shows the new name at the next re-read', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...signedIn })
      listing()
      mount()
      const line = await screen.findByTestId('sandbox-signin-line')
      await waitFor(() => expect(line.textContent).toBe(`Signed in to Kiro as ${EMAIL}.`))
      user.value = { email: 'test1@example.com' } // the server's answer after the switch
      await vi.advanceTimersByTimeAsync(SANDBOX_SIGNED_IN_USER_REFRESH_MS)
      await waitFor(() => expect(line.textContent).toBe('Signed in to Kiro as test1@example.com.'))
    } finally {
      vi.useRealTimers()
    }
  })

  it('not signed in: the account name is never asked for', async () => {
    vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...idle })
    listing()
    mount()
    await screen.findByTestId('sandbox-signin-line')
    expect(managedEnvApi.sandboxSignedInUser).not.toHaveBeenCalled()
  })

  it.each([
    ['a failed read (the lane off: 404)', 'fail'],
    ['an answer with no sign_in field', 'absent'],
    ['another identity or start URL', 'required'],
  ] as const)('Create stays off on %s', async (_label, how) => {
    const spy = vi.spyOn(managedEnvApi, 'sandboxRowLifecycle')
    if (how === 'fail') spy.mockRejectedValue(new Error('not found'))
    else if (how === 'absent') spy.mockResolvedValue({ row_lifecycle: true })
    else spy.mockResolvedValue({ ...idle })
    listing()
    const launch = mount()
    const line = await screen.findByTestId('sandbox-signin-line')
    expect(line.textContent).toBe(REQUIRED)
    const create = screen.getByTestId('sandbox-create') as HTMLButtonElement
    expect(create.disabled).toBe(true)
    fireEvent.click(create)
    expect(launch).not.toHaveBeenCalled()
  })

  it.each([['signed_in'], ['signin_required']] as const)(
    'local agent off (%s): one plain line, no error list',
    async state => {
      vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue(routeAnswer(state, 'off'))
      listing()
      mount()
      const off = await screen.findByTestId('sandbox-local-agent-off')
      const text = 'The local agent is off while you are signed in with this sign-in.'
      expect(off.textContent).toBe(text)
      expect(off.closest('[role="alert"]')).toBeNull()
      expect(screen.queryAllByRole('alert')).toEqual([])
      expect(screen.getAllByText(text)).toHaveLength(1)
    },
  )

  it('the form reads the row-lifecycle route once', async () => {
    const read = vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...signedIn })
    listing()
    mount()
    await screen.findByTestId('sandbox-signin-line')
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('creates with the name once signed in, with no size', async () => {
    vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...signedIn })
    listing()
    const launch = mount()
    const create = screen.getByTestId('sandbox-create') as HTMLButtonElement
    await waitFor(() => expect(create.disabled).toBe(false))
    fireEvent.change(screen.getByTestId('sandbox-name'), { target: { value: 'My-Box' } })
    fireEvent.click(create)
    expect(launch).toHaveBeenCalledWith({ profile: 'my-box', size_key: '' })
  })

  it('refuses a bad name and a running launch job', async () => {
    vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...signedIn })
    listing()
    mount({ activeJob: { status: 'running' } as never })
    await screen.findByText(`Signed in to Kiro as ${EMAIL}.`)
    expect((screen.getByTestId('sandbox-create') as HTMLButtonElement).disabled).toBe(true)
    expect(sandboxNameProblem('a--b')).not.toBe('')
    expect(sandboxNameProblem('x')).not.toBe('')
    expect(sandboxNameProblem('good-name')).toBe('')
  })

  it('enables buttons by status', () => {
    expect(sandboxActionsFor(item('RUNNING'))).toEqual({ stop: true, resume: false, delete: true })
    expect(sandboxActionsFor(item('STOPPED'))).toEqual({ stop: false, resume: true, delete: true })
    expect(sandboxActionsFor(item('PENDING'))).toEqual({ stop: false, resume: false, delete: false })
    expect(sandboxActionsFor(item('RUNNING', { action: { action: 'stop', running: true, error: '', at: 1 } })))
      .toEqual({ stop: false, resume: false, delete: false })
  })

  it.each([['RUNNING'], ['STOPPED'], ['PENDING']] as const)('draws %s with the right buttons disabled', async status => {
    vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...signedIn })
    listing(item(status))
    mount()
    await screen.findByTestId('sandbox-row-sb-1')
    const dis = (k: string) => (screen.getByTestId(`sandbox-${k}-sb-1`) as HTMLButtonElement).disabled
    expect(dis('stop')).toBe(status !== 'RUNNING')
    expect(dis('resume')).toBe(status !== 'STOPPED')
    expect(dis('delete')).toBe(status === 'PENDING')
    expect(screen.getByTestId('sandbox-status-sb-1').textContent).toBe(status)
  })

  it('stop and resume post straight away; delete asks first', async () => {
    vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...signedIn })
    listing(item('RUNNING'))
    const act = vi.spyOn(managedEnvApi, 'sandboxAction').mockResolvedValue({ ok: true, action: 'x' })
    mount()
    fireEvent.click(await screen.findByTestId('sandbox-stop-sb-1'))
    await waitFor(() => expect(act).toHaveBeenCalledWith('sb-1', 'stop'))
    expect(confirmSpy).not.toHaveBeenCalled()

    confirmAnswer.value = false
    await waitFor(() => expect((screen.getByTestId('sandbox-delete-sb-1') as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByTestId('sandbox-delete-sb-1'))
    await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(1))
    expect(act).not.toHaveBeenCalledWith('sb-1', 'delete')

    confirmAnswer.value = true
    fireEvent.click(screen.getByTestId('sandbox-delete-sb-1'))
    await waitFor(() => expect(act).toHaveBeenCalledWith('sb-1', 'delete'))
    expect(confirmSpy).toHaveBeenCalledTimes(2)
  })

  it('shows a route error and a failed last action', async () => {
    vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...signedIn })
    listing(item('STOPPED', { action: { action: 'resume', running: false, error: 'resume failed upstream', at: 1 } }))
    vi.spyOn(managedEnvApi, 'sandboxAction').mockRejectedValue(new Error('another action is still running on this sandbox'))
    mount()
    expect((await screen.findByTestId('sandbox-action-error-sb-1')).textContent).toBe('resume failed upstream')
    fireEvent.click(screen.getByTestId('sandbox-resume-sb-1'))
    expect(await screen.findByText('another action is still running on this sandbox')).toBeTruthy()
  })

  it('shows a list error', async () => {
    vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...signedIn })
    vi.spyOn(managedEnvApi, 'sandboxList').mockRejectedValue(new Error('list down'))
    mount()
    expect(await screen.findByText('list down')).toBeTruthy()
  })

  it('says so when there are none', async () => {
    vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...signedIn })
    listing()
    mount()
    expect((await screen.findByTestId('sandbox-empty')).textContent).toBe('No sandboxes yet.')
  })

  it('re-reads the list when sign-in turns signed_in, and not on other changes', async () => {
    let state: SandboxSignIn['state'] = 'signin_required'
    let localAgent: SandboxSignIn['local_agent'] = 'on'
    vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockImplementation(async () => routeAnswer(state, localAgent))
    vi.spyOn(managedEnvApi, 'sandboxSignedInUser').mockResolvedValue({ email: EMAIL })
    const spy = vi.spyOn(managedEnvApi, 'sandboxList').mockResolvedValue({ sandboxes: [], error: '', row_lifecycle: true })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <SandboxLaunchForm provisioner={provisioner} launch={vi.fn()} launching={false} disabled={false} activeJob={null} />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
    // still signin_required (only the local agent moved): no list read
    localAgent = 'off'
    await qc.invalidateQueries({ queryKey: SANDBOX_ROW_LIFECYCLE_QUERY_KEY })
    await screen.findByTestId('sandbox-local-agent-off')
    expect(spy).toHaveBeenCalledTimes(1)
    // signin_required -> signed_in: one list read
    state = 'signed_in'
    await qc.invalidateQueries({ queryKey: SANDBOX_ROW_LIFECYCLE_QUERY_KEY })
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2))
  })

  it('after a Delete, keeps re-reading until the row is GONE or absent', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...signedIn })
      const spy = vi.spyOn(managedEnvApi, 'sandboxList')
        .mockResolvedValueOnce({ sandboxes: [item('RUNNING')], error: '', row_lifecycle: true })
        .mockResolvedValueOnce({ sandboxes: [item('RUNNING')], error: '', row_lifecycle: true })
        .mockResolvedValue({ sandboxes: [item('GONE')], error: '', row_lifecycle: true })
      vi.spyOn(managedEnvApi, 'sandboxAction').mockResolvedValue({ ok: true, action: 'delete' })
      mount()
      fireEvent.click(await screen.findByTestId('sandbox-delete-sb-1'))
      await waitFor(() => expect(spy).toHaveBeenCalledTimes(2)) // the post-Delete read: still RUNNING
      await vi.advanceTimersByTimeAsync(3_100) // the 3 s follow-up poll sees GONE
      await waitFor(() => expect(spy).toHaveBeenCalledTimes(3))
      await waitFor(() => expect(screen.getByTestId('sandbox-status-sb-1').textContent).toBe('GONE'))
      await vi.advanceTimersByTimeAsync(10_000) // GONE is terminal: back to the slow poll
      expect(spy).toHaveBeenCalledTimes(3)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a GONE row offers no actions', async () => {
    expect(sandboxActionsFor(item('GONE'))).toEqual({ stop: false, resume: false, delete: false })
    vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...signedIn })
    listing(item('GONE'))
    mount()
    await screen.findByTestId('sandbox-row-sb-1')
    expect(screen.queryByTestId('sandbox-stop-sb-1')).toBeNull()
    expect(screen.queryByTestId('sandbox-resume-sb-1')).toBeNull()
    expect(screen.queryByTestId('sandbox-delete-sb-1')).toBeNull()
  })

  it('a sandbox another client made shows the not-set-up line and no buttons', async () => {
    for (const status of ['RUNNING', 'STOPPED', 'FAILED']) {
      expect(sandboxActionsFor(item(status, { own: false }))).toEqual({ stop: false, resume: false, delete: false })
    }
    vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...signedIn })
    listing(item('RUNNING', { own: false }), item('RUNNING', { sandbox_id: 'sb-2', own: true }))
    mount()
    await screen.findByTestId('sandbox-row-sb-1')
    expect(screen.getByTestId('sandbox-not-set-up-sb-1').textContent).toBe("Crew didn't set this sandbox up.")
    expect(screen.queryByTestId('sandbox-stop-sb-1')).toBeNull()
    expect(screen.queryByTestId('sandbox-resume-sb-1')).toBeNull()
    expect(screen.queryByTestId('sandbox-delete-sb-1')).toBeNull()
    // the own one beside it keeps its buttons and has no line
    expect(screen.getByTestId('sandbox-stop-sb-2')).toBeTruthy()
    expect(screen.getByTestId('sandbox-delete-sb-2')).toBeTruthy()
    expect(screen.queryByTestId('sandbox-not-set-up-sb-2')).toBeNull()
  })

  it('an item with no own field is drawn as an own one', async () => {
    const plain = item('RUNNING')
    expect('own' in plain).toBe(false)
    expect(sandboxActionsFor(plain)).toEqual({ stop: true, resume: false, delete: true })
    vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...signedIn })
    listing(plain)
    mount()
    await screen.findByTestId('sandbox-row-sb-1')
    expect(screen.queryByTestId('sandbox-other-client-sb-1')).toBeNull()
    expect(screen.getByTestId('sandbox-stop-sb-1')).toBeTruthy()
    expect(screen.getByTestId('sandbox-resume-sb-1')).toBeTruthy()
    expect(screen.getByTestId('sandbox-delete-sb-1')).toBeTruthy()
  })
})

async function ready() {
  vi.spyOn(managedEnvApi, 'sandboxRowLifecycle').mockResolvedValue({ ...signedIn })
  vi.spyOn(managedEnvApi, 'netcfgStepStatus').mockResolvedValue({
    code: 'status', ok: true, networkAccess: 'COMMON_DEPENDENCIES', customDomains: [],
    needsAgreement: false, canRestore: false, dialog: '',
  })
}

async function launchOn() {
  const create = screen.getByTestId('sandbox-create') as HTMLButtonElement
  await waitFor(() => expect(create.disabled).toBe(false))
  return create
}

describe('the one Create form', () => {
  it('every field has its default, and one click launches with them, sending only the name', async () => {
    await ready()
    listing()
    const launch = mount()
    expect((screen.getByTestId('sandbox-name') as HTMLInputElement).value).toBe('sandbox')
    expect((screen.getByTestId('sandbox-create-idle') as HTMLInputElement).value).toBe('60')
    expect(unitTrigger().textContent).toBe('minutes')
    fireEvent.click(await launchOn())
    expect(launch).toHaveBeenCalledTimes(1)
    expect(launch).toHaveBeenCalledWith({ profile: 'sandbox', size_key: '' })
    expect(screen.queryByTestId('sandbox-netcfg-ask')).toBeNull() // nothing asked after Launch
  })

  it('the values sent are the ones the form shows', async () => {
    await ready()
    listing()
    const launch = mount()
    fireEvent.change(screen.getByTestId('sandbox-name'), { target: { value: 'crew-sandbox-03' } })
    fireEvent.change(screen.getByTestId('sandbox-create-idle'), { target: { value: '2' } })
    await pickUnit('hours')
    fireEvent.click(await launchOn())
    expect(launch).toHaveBeenCalledWith({ profile: 'crew-sandbox-03', size_key: 'idle=120' })
  })

  it('there is no extra storage field, and Launch never sends a volume', async () => {
    await ready()
    listing()
    const launch = mount()
    expect(screen.queryByTestId('sandbox-create-volume')).toBeNull()
    expect(screen.queryByText(/Extra storage/)).toBeNull()
    fireEvent.change(screen.getByTestId('sandbox-create-idle'), { target: { value: '3' } })
    await pickUnit('days')
    fireEvent.click(await launchOn())
    const sent = launch.mock.calls[0][0] as { size_key: string }
    expect(sent.size_key).toBe('idle=4320')
    expect(sent.size_key).not.toContain('volume')
  })

  it('after a Create met idle stop off, the field is off and the form says Launch again', async () => {
    await ready()
    vi.spyOn(managedEnvApi, 'sandboxList').mockResolvedValue({ sandboxes: [], error: '', row_lifecycle: true, idle_off: true })
    const relaunch = 'Idle stop is off here, so the default applies. Launch again.'
    const failed = { id: 'j1', status: 'failed', error: relaunch, steps: [] } as never
    const launch = mount({ activeJob: failed })
    await waitFor(() => expect(screen.getByTestId('sandbox-create-idle-line').textContent).toBe(relaunch))
    expect((screen.getByTestId('sandbox-create-idle') as HTMLInputElement).disabled).toBe(true)
    fireEvent.click(await launchOn())
    expect(launch).toHaveBeenCalledWith({ profile: 'sandbox', size_key: '' }) // no timeout sent
  })

  it('a value outside its range turns Launch off and says why', async () => {
    await ready()
    listing()
    const launch = mount()
    await launchOn()
    fireEvent.change(screen.getByTestId('sandbox-create-idle'), { target: { value: '10' } })
    expect(screen.getByText('Use 15 minutes to 7 days.')).toBeTruthy()
    expect((screen.getByTestId('sandbox-create') as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByTestId('sandbox-create'))
    expect(launch).not.toHaveBeenCalled()
  })

  it('with idle stop off, the idle field is off, says so, and is never sent', async () => {
    await ready()
    vi.spyOn(managedEnvApi, 'sandboxList').mockResolvedValue({ sandboxes: [], error: '', row_lifecycle: true, idle_off: true })
    const launch = mount()
    await screen.findByText('Idle stop is off here, so the default applies.')
    expect((screen.getByTestId('sandbox-create-idle') as HTMLInputElement).disabled).toBe(true)
    fireEvent.click(await launchOn())
    expect(launch).toHaveBeenCalledWith({ profile: 'sandbox', size_key: '' })
  })

  it('the checks sit on the right: signed in as the user, and the network setting', async () => {
    await ready()
    listing()
    mount()
    const checks = await screen.findByTestId('sandbox-create-checks')
    expect(checks.textContent).toContain('Before you launch')
    await waitFor(() => expect(screen.getByTestId('sandbox-check-signin').textContent).toContain(`Signed in to Kiro as ${EMAIL}.`))
    await waitFor(() =>
      expect(screen.getByTestId('sandbox-check-network-line').textContent).toBe(
        'Your network setting lets Crew download its runtime.',
      ),
    )
  })

  it('sends a default as absent, which the service reads the same', () => {
    expect(sandboxCreateOptions(60, false)).toBe('')
    expect(sandboxCreateOptions(15, false)).toBe('idle=15')
    expect(sandboxCreateOptions(10080, false)).toBe('idle=10080')
    expect(sandboxCreateOptions(120, true)).toBe('') // idle stop off: never sent
  })
})

describe('the shown name: "managed environment", not "persistent sandbox"', () => {
  it('the Create form is titled New managed environment, and no form text says persistent sandbox', async () => {
    await ready()
    listing()
    mount()
    expect(await screen.findByText('New managed environment')).toBeTruthy()
    for (const key of Object.keys(CREATE_TEXT) as (keyof typeof CREATE_TEXT)[]) {
      expect(String(CREATE_TEXT[key])).not.toMatch(/persistent[\s_-]*sandbox/i)
    }
  })
})
