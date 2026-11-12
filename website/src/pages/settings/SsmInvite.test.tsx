import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../../api/client'
import type { SsmInvite, SsmStatus } from '../../api/sharedCrewSsm'
import { ssmInviteFor, waitingSsmInvites } from '../../api/sharedCrewSsm'
import type { PeopleRow } from '../../api/sharedCrewUi'
import type { TailscaleInvitesStatus } from '../../api/sharedCrewInvites'
import { InviteEntry } from './TailscaleInvites'
import { SsmInviteRow, buildSsmInviteMessage } from './SsmInvite'
import { CopyInviteMessage, RevokeControl } from './TailscaleInvites'

vi.mock('../../api/client', () => ({
  api: {
    tailscaleInvites: vi.fn(),
    sharedCrewSsm: vi.fn(),
    sharedCrewSsmCreateInvite: vi.fn(),
    sharedCrewSsmRevokeInvite: vi.fn(),
    sharedCrewAccess: vi.fn(),
    sharedCrewAckFirstShare: vi.fn(),
    sharedCrewCapabilities: vi.fn(),
    sharedCrewPeople: vi.fn(),
  },
}))
const copy = vi.hoisted(() => vi.fn<(text: string) => Promise<boolean>>())
vi.mock('../../utils/clipboard', () => ({ copyToClipboard: copy }))

const tailscale = vi.mocked(api.tailscaleInvites)
const ssmRead = vi.mocked(api.sharedCrewSsm)
const ssmCreate = vi.mocked(api.sharedCrewSsmCreateInvite)
const ssmRevoke = vi.mocked(api.sharedCrewSsmRevokeInvite)
const accessRead = vi.mocked(api.sharedCrewAccess)
const ack = vi.mocked(api.sharedCrewAckFirstShare)
const capsRead = vi.mocked(api.sharedCrewCapabilities)
const peopleRead = vi.mocked(api.sharedCrewPeople)

const COMMAND = 'aws ssm start-session --target i-0123456789abcdef0 --document-name Kiro-Crew-Shared-Crew-5477 --parameters localPortNumber=5477'

function ssmStatus(over: Partial<SsmStatus> = {}): SsmStatus {
  return {
    state: 'active', reason: '', port: 5477, target: { instance_id: 'i-0123456789abcdef0', region: 'us-east-1' },
    command: COMMAND, local_url: 'http://localhost:5477/', invites: [], ...over,
  }
}

function ssmInvite(over: Partial<SsmInvite> = {}): SsmInvite {
  return { id: 'abc123', alias: 'Dana', created_at: 1_700_000_000, joined: false, principal_id: 'p_ssm_abc123', ...over }
}

function tailscaleStatus(): TailscaleInvitesStatus {
  return {
    connected: true, tailnet: 'example.ts.net', key_expires: '2099-01-01T00:00:00Z', key_expired: false, key_expires_soon: false,
    admin_share_url: '', admin_keys_url: '', invites: [], members: [], last_sync_at: 0, sync: {},
  }
}

function person(id: string): PeopleRow {
  return { principal: { id, kind: 'human', display: { name: 'Dana', login: '', captured_at: null } }, sources: ['observed'] } as unknown as PeopleRow
}

// The Invite entry reads capabilities first and the invites only after that settles, and the
// SSM pane waits on its own read, so a lookup spans a chain of queries. A generous ceiling
// keeps a loaded runner from failing a healthy chain on the default lookup deadline.
const CHAIN_TIMEOUT_MS = 5_000

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><InviteEntry /></QueryClientProvider>)
}

async function openDialog() {
  mount()
  fireEvent.click(await screen.findByTestId('share-hero-invite', {}, { timeout: CHAIN_TIMEOUT_MS }))
  await screen.findByTestId('invite-dialog', {}, { timeout: CHAIN_TIMEOUT_MS })
}

beforeEach(() => {
  vi.resetAllMocks()
  accessRead.mockResolvedValue(null)
  copy.mockResolvedValue(true)
  capsRead.mockResolvedValue({} as never)
  peopleRead.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [] } as never)
})

describe('waiting SSM invites', () => {
  it('lists an unopened invite and hides a joined one whose person has a row', () => {
    const open = ssmInvite({ id: 'a', principal_id: 'p_a' })
    const joined = ssmInvite({ id: 'b', principal_id: 'p_b', joined: true })
    expect(waitingSsmInvites([person('p_b')], [open, joined]).map(i => i.id)).toEqual(['a'])
  })

  it('keeps a joined invite whose person has not been seen since the gateway started, so it can be removed', () => {
    const joined = ssmInvite({ id: 'b', principal_id: 'p_b', joined: true })
    expect(waitingSsmInvites([], [joined]).map(i => i.id)).toEqual(['b'])
  })

  it('finds the invite a joined person came in with', () => {
    const joined = ssmInvite({ id: 'b', principal_id: 'p_b', joined: true })
    expect(ssmInviteFor(person('p_b'), [ssmInvite(), joined])?.id).toBe('b')
    expect(ssmInviteFor(person('p_other'), [joined])).toBeUndefined()
  })
})

describe('SsmInviteRow', () => {
  it('says when it was made and that the link is not shown again', () => {
    const qc = new QueryClient()
    render(<QueryClientProvider client={qc}><ul><SsmInviteRow invite={ssmInvite()} /></ul></QueryClientProvider>)
    const row = screen.getByTestId('ssm-invite-abc123')
    expect(row).toHaveTextContent('Created')
    expect(row).not.toHaveTextContent('Sent')
    expect(row).toHaveTextContent('Link not shown again')
    expect(row).not.toHaveTextContent('Pending')
    // Only the text fades: Remove reads as live, as on the People rows.
    expect(row).not.toHaveClass('opacity-60')
    expect(screen.getByTestId('invite-revoke').closest('.opacity-60')).toBeNull()
  })

  it('removes after a confirmation', async () => {
    ssmRevoke.mockResolvedValue({ revoked: true })
    const qc = new QueryClient()
    render(<QueryClientProvider client={qc}><RevokeControl inviteId="abc123" name="Dana" transport="ssm" /></QueryClientProvider>)
    fireEvent.click(screen.getByTestId('invite-revoke'))
    expect(ssmRevoke).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('invite-revoke-confirm'))
    await waitFor(() => expect(ssmRevoke).toHaveBeenCalledWith('abc123'))
  })
})

describe('Remove reads the same for both transports', () => {
  it.each(['tailscale', 'ssm'] as const)('%s: Remove, Yes, remove, Cancel', transport => {
    render(<QueryClientProvider client={new QueryClient()}><RevokeControl inviteId="x" name="Dana" transport={transport} /></QueryClientProvider>)
    expect(screen.getByTestId('invite-revoke')).toHaveTextContent('Remove')
    fireEvent.click(screen.getByTestId('invite-revoke'))
    expect(screen.getByTestId('invite-revoke-confirm')).toHaveTextContent('Yes, remove')
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument()
  })
})

describe('Copy invite message is one component', () => {
  it('reads the same and copies a finished message as it is', async () => {
    copy.mockResolvedValue(true)
    const onCopied = vi.fn()
    render(<QueryClientProvider client={new QueryClient()}><CopyInviteMessage message={"line one\nline two"} testId="m" onCopied={onCopied} /></QueryClientProvider>)
    expect(screen.getByTestId('m')).toHaveTextContent('Copy SSM invite')
    fireEvent.click(screen.getByTestId('m'))
    await waitFor(() => expect(copy).toHaveBeenCalledWith('line one\nline two'))
    expect(onCopied).toHaveBeenCalledTimes(1)
  })
})

describe('a failed SSM status read', () => {
  it('shows a notice with a retry on the page and in the dialog instead of hiding the SSM invites', async () => {
    tailscale.mockResolvedValue(tailscaleStatus())
    ssmRead.mockRejectedValue(new Error('boom'))
    mount()
    expect(await screen.findByTestId('people-ssm-error', {}, { timeout: CHAIN_TIMEOUT_MS })).toBeInTheDocument()
    fireEvent.click(await screen.findByTestId('share-hero-invite', {}, { timeout: CHAIN_TIMEOUT_MS }))
    expect(await screen.findByTestId('invite-dialog-ssm-error', {}, { timeout: CHAIN_TIMEOUT_MS })).toBeInTheDocument()
    ssmRead.mockResolvedValue(ssmStatus())
    fireEvent.click(screen.getAllByRole('button', { name: 'Try again' })[0]!)
    await waitFor(() => expect(screen.queryByTestId('invite-dialog-ssm-error')).toBeNull())
    expect(ssmRead.mock.calls.length).toBeGreaterThan(1)
  })

  it('shows nothing when the read answers that SSM is not available', async () => {
    tailscale.mockResolvedValue(tailscaleStatus())
    ssmRead.mockResolvedValue(null)
    mount()
    await screen.findByTestId('share-hero-invite', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(screen.queryByTestId('people-ssm-error')).toBeNull()
  })
})

describe('the SSM status line', () => {
  it.each([['bind_failed', 'The SSM port is already in use.'], ['port_in_use', 'The SSM port is the same as the dashboard port.']])(
    'shows %s as an error notice, not as setup text',
    async (reason, text) => {
      tailscale.mockResolvedValue(null)
      ssmRead.mockResolvedValue(ssmStatus({ state: 'unavailable', reason, target: null, command: '', local_url: '', invites: [] }))
      await openDialog()
      const line = await screen.findByTestId('ssm-reason', {}, { timeout: CHAIN_TIMEOUT_MS })
      expect(line).toHaveTextContent(text)
      expect(line).toHaveAttribute('role', 'alert')
    },
  )
})

describe('the Invite dialog and its transports', () => {
  it('shows tabs only when both transports are on', async () => {
    tailscale.mockResolvedValue(tailscaleStatus())
    ssmRead.mockResolvedValue(ssmStatus())
    await openDialog()
    expect(await screen.findByTestId('invite-transport-ssm', {}, { timeout: CHAIN_TIMEOUT_MS })).toBeInTheDocument()
    expect(screen.getByTestId('invite-transport-tailscale')).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId('invite-form')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('invite-transport-ssm'))
    expect(await screen.findByTestId('ssm-tab', {}, { timeout: CHAIN_TIMEOUT_MS })).toBeVisible()
    expect(screen.getByTestId('invite-form')).not.toBeVisible()
  })

  it('keeps a link shown once when the owner switches tabs and back', async () => {
    tailscale.mockResolvedValue(tailscaleStatus())
    ssmRead.mockResolvedValue(ssmStatus())
    ssmCreate.mockResolvedValue({ invite: ssmInvite(), url: 'http://localhost:5477/?ssm_invite=abc123.secret', command: COMMAND, local_url: 'http://localhost:5477/' })
    await openDialog()
    fireEvent.click(await screen.findByTestId('invite-transport-ssm', {}, { timeout: CHAIN_TIMEOUT_MS }))
    fireEvent.click(await screen.findByTestId('ssm-make-link', {}, { timeout: CHAIN_TIMEOUT_MS }))
    await screen.findByTestId('ssm-created-link', {}, { timeout: CHAIN_TIMEOUT_MS })
    fireEvent.click(screen.getByTestId('invite-transport-tailscale'))
    fireEvent.click(screen.getByTestId('invite-transport-ssm'))
    expect(screen.getByTestId('ssm-created-link')).toBeVisible()
  })

  it('shows no tabs and the Tailscale form when SSM is off', async () => {
    tailscale.mockResolvedValue(tailscaleStatus())
    ssmRead.mockResolvedValue(ssmStatus({ state: 'off', reason: 'disabled' }))
    await openDialog()
    expect(await screen.findByTestId('invite-form', {}, { timeout: CHAIN_TIMEOUT_MS })).toBeInTheDocument()
    expect(screen.queryByTestId('invite-transport-ssm')).toBeNull()
  })

  it('shows the SSM tab alone, with no tabs, when Tailscale invites are not available', async () => {
    tailscale.mockResolvedValue(null)
    ssmRead.mockResolvedValue(ssmStatus())
    await openDialog()
    expect(await screen.findByTestId('ssm-tab', {}, { timeout: CHAIN_TIMEOUT_MS })).toBeInTheDocument()
    expect(screen.queryByTestId('invite-transport-tailscale')).toBeNull()
  })

  it('offers no Invite button to someone neither invite read answered for', async () => {
    tailscale.mockResolvedValue(null)
    ssmRead.mockResolvedValue(null)
    mount()
    await waitFor(() => expect(ssmRead).toHaveBeenCalled())
    expect(screen.queryByTestId('share-hero-invite')).toBeNull()
  })

  it('names why SSM cannot start, with the guide, and offers no form', async () => {
    tailscale.mockResolvedValue(null)
    ssmRead.mockResolvedValue(ssmStatus({ state: 'unavailable', reason: 'no_target', command: '', local_url: '', target: null }))
    await openDialog()
    const noNode = await screen.findByTestId('ssm-reason', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(noNode).toHaveTextContent('No SSM node found for this machine.')
    // A missing node is a state of the machine, not a failure: plain text.
    expect(noNode).not.toHaveAttribute('role', 'alert')
    expect(screen.getByTestId('ssm-guide')).toHaveAttribute('href', expect.stringContaining('#share-one-crew-over-aws-ssm'))
    expect(screen.queryByTestId('ssm-form')).toBeNull()
  })

  it('keeps an earlier link shown when another is made', async () => {
    tailscale.mockResolvedValue(null)
    ssmRead.mockResolvedValue(ssmStatus())
    ssmCreate
      .mockResolvedValueOnce({ invite: ssmInvite({ id: 'one' }), url: 'http://localhost:5477/?ssm_invite=one.aaa', command: COMMAND, local_url: 'http://localhost:5477/' })
      .mockResolvedValueOnce({ invite: ssmInvite({ id: 'two' }), url: 'http://localhost:5477/?ssm_invite=two.bbb', command: COMMAND, local_url: 'http://localhost:5477/' })
    await openDialog()
    fireEvent.click(await screen.findByTestId('ssm-make-link', {}, { timeout: CHAIN_TIMEOUT_MS }))
    await screen.findByText('http://localhost:5477/?ssm_invite=one.aaa')
    fireEvent.click(screen.getByTestId('ssm-make-link'))
    await screen.findByText('http://localhost:5477/?ssm_invite=two.bbb')
    expect(screen.getByText('http://localhost:5477/?ssm_invite=one.aaa')).toBeInTheDocument()
  })

  it('stays on the SSM pane, with its draft, when the Tailscale read lands after the dialog opened', async () => {
    let resolveTailscale: (value: TailscaleInvitesStatus) => void = () => {}
    tailscale.mockReturnValue(new Promise(resolve => { resolveTailscale = resolve }))
    ssmRead.mockResolvedValue(ssmStatus())
    mount()
    // Neither Tailscale nor SSM has answered the Invite button's read yet; SSM does first.
    fireEvent.click(await screen.findByTestId('share-hero-invite', {}, { timeout: CHAIN_TIMEOUT_MS }))
    fireEvent.change(await screen.findByTestId('ssm-alias', {}, { timeout: CHAIN_TIMEOUT_MS }), { target: { value: 'Draft name' } })
    resolveTailscale(tailscaleStatus())
    await screen.findByTestId('invite-transport-ssm', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(screen.getByTestId('ssm-alias')).toHaveValue('Draft name')
    expect(screen.getByTestId('invite-transport-ssm')).toHaveAttribute('aria-selected', 'true')
  })

  it('makes a link with the Tailscale labels and shows it once, with the message to copy', async () => {
    tailscale.mockResolvedValue(null)
    ssmRead.mockResolvedValue(ssmStatus())
    ssmCreate.mockResolvedValue({ invite: ssmInvite(), url: 'http://localhost:5477/?ssm_invite=abc123.secret', command: COMMAND, local_url: 'http://localhost:5477/' })
    peopleRead.mockResolvedValue({
      schema_version: 1, directory: { state: 'unavailable' }, usage_available: false,
      people: [{ principal: { id: 'p_o', kind: 'human', display: { name: 'Mac Studio', login: '', captured_at: null } }, sources: ['observed'], is_viewer: true, is_owner: true }],
    } as never)
    await openDialog()
    expect(await screen.findByText('Name to show (optional)')).toBeInTheDocument()
    expect(screen.getByTestId('ssm-make-link')).toHaveTextContent('Make link')
    fireEvent.change(screen.getByTestId('ssm-alias'), { target: { value: ' Dana ' } })
    fireEvent.click(screen.getByTestId('ssm-make-link'))
    await waitFor(() => expect(ssmCreate).toHaveBeenCalledWith('Dana'))
    expect(await screen.findByTestId('ssm-created-link', {}, { timeout: CHAIN_TIMEOUT_MS })).toHaveTextContent('http://localhost:5477/?ssm_invite=abc123.secret')
    // The bare link opens nothing on the teammate's machine without the forward, so the only copy action is the full invite.
    expect(screen.queryByTestId('ssm-copy-link')).toBeNull()
    expect(screen.getByTestId('ssm-copy-message')).toHaveTextContent('Copy SSM invite')
    await waitFor(() => expect(peopleRead).toHaveBeenCalled())
    fireEvent.click(screen.getByTestId('ssm-copy-message'))
    await waitFor(() => expect(copy).toHaveBeenCalledTimes(1))
    expect(copy.mock.calls[0][0].split('\n')).toEqual([
      'Mac Studio invited you to Kiro Crew.',
      `1. Forward the port: ${COMMAND}`,
      '2. Open Crew: http://localhost:5477/?ssm_invite=abc123.secret',
      expect.stringContaining('#share-one-crew-over-aws-ssm'),
    ])
  })
})

describe('first-share consent on the SSM route', () => {
  const needsAck = { first_share: { needs_ack: true, sessions: 3, memory_items: 7, host: { state: 'available', reason: '', backend: '', laptop: false, laptop_reason: '' } } }

  it('asks before the first invite, with the SSM audience and stop-sharing lines, and makes nothing if declined', async () => {
    tailscale.mockResolvedValue(null)
    ssmRead.mockResolvedValue(ssmStatus())
    accessRead.mockResolvedValue(needsAck as never)
    await openDialog()
    fireEvent.click(await screen.findByTestId('ssm-make-link', {}, { timeout: CHAIN_TIMEOUT_MS }))
    expect(await screen.findByTestId('first-share-audience', {}, { timeout: CHAIN_TIMEOUT_MS })).toHaveTextContent('Anyone who has an invite link you make can open this Crew.')
    expect(screen.getByTestId('first-share-stop')).toHaveTextContent('To stop sharing, remove them in People.')
    expect(screen.getByTestId('first-share-body')).toHaveTextContent('3')
    expect(screen.queryByText(/tailnet/i)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByTestId('first-share-body')).toBeNull())
    expect(ssmCreate).not.toHaveBeenCalled()
    expect(ack).not.toHaveBeenCalled()
  })

  it('acknowledges once and then makes the link', async () => {
    tailscale.mockResolvedValue(null)
    ssmRead.mockResolvedValue(ssmStatus())
    accessRead.mockResolvedValue(needsAck as never)
    ack.mockResolvedValue(undefined)
    ssmCreate.mockResolvedValue({ invite: ssmInvite(), url: 'http://localhost:5477/?ssm_invite=abc123.secret', command: COMMAND, local_url: 'http://localhost:5477/' })
    await openDialog()
    fireEvent.click(await screen.findByTestId('ssm-make-link', {}, { timeout: CHAIN_TIMEOUT_MS }))
    await screen.findByTestId('first-share-body', {}, { timeout: CHAIN_TIMEOUT_MS })
    fireEvent.click(screen.getByRole('button', { name: 'Share it' }))
    await waitFor(() => expect(ack).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(ssmCreate).toHaveBeenCalledTimes(1))
  })
})

describe('the invite message', () => {
  it('leaves the guide line out when there is no guide URL', () => {
    const lines = buildSsmInviteMessage({ owner: 'Mac', command: 'cmd', url: 'http://localhost:5477/', guide: '' }).split('\n')
    expect(lines).toHaveLength(3)
  })
})
