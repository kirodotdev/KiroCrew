import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import userEvent from '@testing-library/user-event'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import type { TailscaleInvite, TailscaleInvitesStatus } from '../../api/sharedCrewInvites'
import { parseEmails, looksLikeApiKey } from '../../api/sharedCrewInvites'
import { TailscaleInvites, inviteErrorText, tailscaleInvitesKey } from './TailscaleInvites'

vi.mock('../../api/client', () => ({
  api: {
    tailscaleInvites: vi.fn(),
    tailscaleConnect: vi.fn(),
    tailscaleDisconnect: vi.fn(),
    tailscaleCreateInvites: vi.fn(),
    tailscaleResendInvite: vi.fn(),
    tailscaleRevokeInvite: vi.fn(),
    tailscaleUploadAvatar: vi.fn(),
    sharedCrewAccess: vi.fn(),
    sharedCrewAckFirstShare: vi.fn(),
  },
}))
const copy = vi.hoisted(() => vi.fn<(text: string) => Promise<boolean>>())
vi.mock('../../utils/clipboard', () => ({ copyToClipboard: copy }))
const read = vi.mocked(api.tailscaleInvites)
const connect = vi.mocked(api.tailscaleConnect)
const create = vi.mocked(api.tailscaleCreateInvites)
const revoke = vi.mocked(api.tailscaleRevokeInvite)
const disconnect = vi.mocked(api.tailscaleDisconnect)
const accessRead = vi.mocked(api.sharedCrewAccess)
const ack = vi.mocked(api.sharedCrewAckFirstShare)

const KEY = 'tskey-api-kAbC123CNTRL-Zz9yYxXwWvV'

function status(over: Partial<TailscaleInvitesStatus> = {}): TailscaleInvitesStatus {
  return {
    connected: true, tailnet: 'example.ts.net', key_expires: '2099-01-01T00:00:00Z', key_expired: false, key_expires_soon: false,
    admin_share_url: 'https://login.tailscale.com/admin/machines', admin_keys_url: 'https://login.tailscale.com/admin/settings/keys',
    invites: [], members: [], last_sync_at: 0, sync: {}, ...over,
  }
}

function invite(over: Partial<TailscaleInvite> = {}): TailscaleInvite {
  return { id: '101', kind: 'email', email: 'ann@example.com', multi_use: false, alias: '', avatar_id: '', created_at: 1_700_000_000, status: 'pending', accepted_login: '', ...over }
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><TailscaleInvites /></QueryClientProvider>)
}

beforeEach(() => { vi.resetAllMocks(); accessRead.mockResolvedValue(null); copy.mockResolvedValue(true) })

describe('TailscaleInvites', () => {
  it('without a key: shows the admin-console share path and the one-time key form', async () => {
    read.mockResolvedValue(status({ connected: false }))
    mount()
    expect(await screen.findByTestId('invite-connect')).toBeInTheDocument()
    expect(screen.getByTestId('invite-connect')).toHaveTextContent('For the direct link, Tailscale access rules apply.')
    expect(screen.getByTestId('invite-connect')).toHaveTextContent('If local authorization is on')
    expect(screen.getByTestId('invite-admin-share')).toHaveAttribute('href', 'https://login.tailscale.com/admin/machines')
    expect(screen.getByTestId('invite-admin-keys')).toHaveAttribute('href', 'https://login.tailscale.com/admin/settings/keys')
    const input = screen.getByTestId('invite-key-input')
    expect(input).toHaveAttribute('type', 'password')
    expect(screen.getByTestId('invite-key-connect')).toBeDisabled()
    fireEvent.change(input, { target: { value: 'tskey-client-abc-def' } })
    expect(screen.getByTestId('invite-key-connect')).toBeDisabled()
  })

  it('connects once and clears the key from the field', async () => {
    read.mockResolvedValue(status({ connected: false }))
    connect.mockResolvedValue(status())
    mount()
    const input = await screen.findByTestId('invite-key-input')
    fireEvent.change(input, { target: { value: KEY } })
    fireEvent.click(screen.getByTestId('invite-key-connect'))
    await waitFor(() => expect(connect).toHaveBeenCalledWith(KEY))
    expect(await screen.findByTestId('invite-connected')).toBeInTheDocument()
    expect(screen.getByText('Connected to example.ts.net')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('Zz9yYxXwWvV')
  })

  it('names a refused key without a sign-out', async () => {
    read.mockResolvedValue(status({ connected: false }))
    connect.mockRejectedValue(new ApiError(502, 'x', JSON.stringify({ code: 'tailscale_auth_failed' })))
    mount()
    fireEvent.change(await screen.findByTestId('invite-key-input'), { target: { value: KEY } })
    fireEvent.click(screen.getByTestId('invite-key-connect'))
    expect(await screen.findByTestId('invite-key-error')).toHaveTextContent('Tailscale did not accept this key')
  })

  it('sends email invites with the alias, single-use, never an exit-node option', async () => {
    read.mockResolvedValue(status())
    create.mockResolvedValue({ invites: [invite()] })
    mount()
    fireEvent.change(await screen.findByTestId('invite-emails'), { target: { value: 'ann@example.com, bo@example.com' } })
    fireEvent.change(screen.getByTestId('invite-alias'), { target: { value: 'Ann' } })
    fireEvent.click(screen.getByTestId('invite-send'))
    await waitFor(() => expect(create).toHaveBeenCalledWith({ emails: ['ann@example.com', 'bo@example.com'], multi_use: false, alias: 'Ann', avatar_id: '' }))
    expect(screen.queryByText(/exit node/i)).toBeNull()
  })

  it('blocks a malformed email before sending, with a field hint and no error notice', async () => {
    read.mockResolvedValue(status())
    mount()
    fireEvent.change(await screen.findByTestId('invite-emails'), { target: { value: 'not-an-email' } })
    expect(screen.getByTestId('invite-email-hint')).toHaveTextContent(inviteErrorText('invalid_email'))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByTestId('invite-send')).toBeDisabled()
  })

  it('a picture of the wrong type is a field hint, not an upload error', async () => {
    read.mockResolvedValue(status())
    mount()
    const file = new File(['x'], 'a.txt', { type: 'text/plain' })
    fireEvent.change(await screen.findByTestId('invite-avatar-input'), { target: { files: [file] } })
    expect(screen.getByTestId('invite-avatar-hint')).toHaveTextContent(inviteErrorText('avatar_type'))
    expect(screen.queryByTestId('invite-avatar-error')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('a failed copy says so through the error notice', async () => {
    read.mockResolvedValue(status())
    create.mockResolvedValue({ invites: [invite({ kind: 'link', email: '', invite_url: 'https://login.tailscale.com/admin/invite/abc' })] })
    copy.mockResolvedValue(false)
    mount()
    fireEvent.click(await screen.findByTestId('invite-mode-link'))
    fireEvent.click(screen.getByTestId('invite-send'))
    await screen.findByTestId('invite-copy-link')
    fireEvent.change(screen.getByTestId('invite-alias'), { target: { value: 'Next invite draft' } })
    fireEvent.click(screen.getByTestId('invite-copy-link'))
    const error = await screen.findByTestId('invite-copy-error')
    expect(error).toBeInTheDocument()
    expect(within(error).queryByRole('button', { name: 'Ask the agent' })).toBeNull()
    expect(screen.getByTestId('invite-alias')).toHaveValue('Next invite draft')
  })

  it.each([
    ['auth_failed', /did not accept this key/],
    ['forbidden', /cannot share machines/],
    ['rate_limited', /slow down/],
    ['backing_off', /slow down/],
  ])('names the bare %s code the status read reports as its sync error', async (error, text) => {
    read.mockResolvedValue(status({ sync: { error } }))
    mount()
    expect(await screen.findByTestId('invite-sync-error')).toHaveTextContent(text)
  })

  it('retries the same picture after a failed upload', async () => {
    read.mockResolvedValue(status())
    const upload = vi.mocked(api.tailscaleUploadAvatar)
    upload.mockRejectedValueOnce(new Error('boom')).mockResolvedValueOnce({ avatar_id: 'second' })
    mount()
    const input = await screen.findByTestId('invite-avatar-input') as HTMLInputElement
    const file = new File(['png'], 'one.png', { type: 'image/png' })
    await userEvent.upload(input, file)
    await screen.findByTestId('invite-avatar-error')
    expect(input.value).toBe('')
    await userEvent.upload(input, file)
    await waitFor(() => expect(upload).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByTestId('invite-avatar-error')).toBeNull())
  })

  it.each(['tailscale_rate_limited', 'tailscale_auth_failed'])('shows a %s synchronization failure without losing the invite draft', async error => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    const current = status({ invites: [invite()] })
    qc.setQueryData(tailscaleInvitesKey, current)
    render(<QueryClientProvider client={qc}><TailscaleInvites /></QueryClientProvider>)
    fireEvent.change(await screen.findByTestId('invite-emails'), { target: { value: 'draft@example.test' } })
    fireEvent.change(screen.getByTestId('invite-alias'), { target: { value: 'Invite draft' } })
    read.mockResolvedValue({ ...current, sync: { error } })
    await act(async () => { await qc.invalidateQueries({ queryKey: tailscaleInvitesKey }) })
    const notice = await screen.findByTestId('invite-sync-error')
    expect(notice).toHaveTextContent(inviteErrorText(error))
    expect(within(notice).queryByRole('button', { name: 'Ask the agent' })).toBeNull()
    expect(screen.getByTestId('invite-emails')).toHaveValue('draft@example.test')
    expect(screen.getByTestId('invite-alias')).toHaveValue('Invite draft')
    expect(screen.getByTestId('invite-101')).toHaveAttribute('data-status', 'pending')
    read.mockResolvedValue(current)
    await act(async () => { await qc.invalidateQueries({ queryKey: tailscaleInvitesKey }) })
    await waitFor(() => expect(screen.queryByTestId('invite-sync-error')).toBeNull())
    expect(screen.getByTestId('invite-alias')).toHaveValue('Invite draft')
  })

  it('makes a link invite and offers to copy it with a warning', async () => {
    read.mockResolvedValue(status())
    create.mockResolvedValue({ invites: [invite({ kind: 'link', email: '', invite_url: 'https://login.tailscale.com/admin/invite/abc' })] })
    mount()
    fireEvent.click(await screen.findByTestId('invite-mode-link'))
    fireEvent.click(screen.getByTestId('invite-send'))
    await waitFor(() => expect(create).toHaveBeenCalledWith({ emails: [], multi_use: false, alias: '', avatar_id: '' }))
    expect(await screen.findByTestId('invite-link-result')).toHaveTextContent('https://login.tailscale.com/admin/invite/abc')
    expect(screen.getByText(/Share it like a password/)).toBeInTheDocument()
  })

  it('lists pending and accepted invites, naming an accept from another account', async () => {
    read.mockResolvedValue(status({
      invites: [
        invite({ id: '1', status: 'pending' }),
        invite({ id: '2', email: 'bo@work.example', status: 'accepted', accepted_login: 'bo@gmail.example', alias: 'Bo' }),
      ],
    }))
    mount()
    expect(await screen.findByTestId('invite-1')).toHaveAttribute('data-status', 'pending')
    expect(screen.getByTestId('invite-2')).toHaveAttribute('data-status', 'accepted')
    expect(screen.getByText('Bo')).toBeInTheDocument()
    expect(screen.getByText('bo@gmail.example')).toBeInTheDocument()
    expect(screen.getByTestId('invite-other-account')).toHaveTextContent('bo@work.example')
    expect(screen.getAllByTestId('invite-resend')).toHaveLength(1)
  })

  it('revokes only after a confirm step', async () => {
    read.mockResolvedValue(status({ invites: [invite({ id: '9', status: 'accepted', accepted_login: 'ann@example.com' })] }))
    revoke.mockResolvedValue({ revoked: '9', member_removed: true })
    mount()
    fireEvent.click(await screen.findByTestId('invite-revoke'))
    expect(revoke).not.toHaveBeenCalled()
    expect(screen.getByText(/With local authorization on, Crew also closes their tabs/)).toBeInTheDocument()
    expect(screen.getByText(/Crew does not stop their work/)).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('invite-revoke-confirm'))
    await waitFor(() => expect(revoke).toHaveBeenCalledWith('9'))
  })

  it('a pending email invite shows only Confirm and Cancel while confirming a revoke', async () => {
    read.mockResolvedValue(status({ invites: [invite({ id: '7' })] }))
    mount()
    expect(await screen.findByTestId('invite-resend')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('invite-revoke'))
    expect(screen.getByTestId('invite-revoke-confirm')).toBeInTheDocument()
    expect(screen.queryByTestId('invite-resend')).toBeNull()
  })

  it('removes the API key only after a confirm, since it cannot be undone', async () => {
    read.mockResolvedValue(status())
    disconnect.mockResolvedValue(status({ connected: false }))
    mount()
    const button = await screen.findByTestId('invite-disconnect')
    fireEvent.click(button)
    expect(button).toBeDisabled()
    expect(disconnect).not.toHaveBeenCalled()
    expect(await screen.findByText(/can't be undone/)).toBeInTheDocument()
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Remove key' }))
    await waitFor(() => expect(disconnect).toHaveBeenCalledTimes(1))
  })

  it('shows a failed key removal', async () => {
    read.mockResolvedValue(status())
    disconnect.mockRejectedValue(new ApiError(500, 'x', JSON.stringify({ code: 'error' })))
    mount()
    fireEvent.click(await screen.findByTestId('invite-disconnect'))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Remove key' }))
    expect(await screen.findByTestId('invite-disconnect-error')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('invite-disconnect')).toBeEnabled())
  })

  it('asks the first-share question before the first invite, then sends', async () => {
    read.mockResolvedValue(status())
    accessRead.mockResolvedValue({
      readable: true, waiting: [], approved: [], seeded: [], first_share_ack: false,
      first_share: { needs_ack: true, sessions: 4, memory_items: 12, host: { state: 'available', reason: '', backend: 'caffeinate', laptop: false, laptop_reason: 'no_battery' } },
    })
    ack.mockResolvedValue(undefined)
    create.mockResolvedValue({ invites: [invite()] })
    mount()
    fireEvent.change(await screen.findByTestId('invite-emails'), { target: { value: 'ann@example.com' } })
    fireEvent.click(screen.getByTestId('invite-send'))
    expect(await screen.findByText('They will see every session (4 now) and Crew memory (12 items).')).toBeInTheDocument()
    expect(create).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Share it' }))
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1))
    expect(ack).toHaveBeenCalledTimes(1)
  })

  it('warns before the key expires and after it has', async () => {
    read.mockResolvedValue(status({ key_expires_soon: true }))
    const view = mount()
    expect(await screen.findByText('Key expires soon')).toBeInTheDocument()
    view.unmount()
    read.mockResolvedValue(status({ key_expired: true }))
    mount()
    expect(await screen.findByText('Key expired')).toBeInTheDocument()
  })

  it('renders nothing for a non-owner or a gateway without the routes', async () => {
    read.mockResolvedValue(null)
    const view = mount()
    await waitFor(() => expect(read).toHaveBeenCalled())
    expect(screen.queryByTestId('tailscale-invites')).toBeNull()
    view.unmount()
    read.mockRejectedValue(new ApiError(403, 'x', JSON.stringify({ code: 'owner_only' })))
    mount()
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
    expect(screen.queryByTestId('tailscale-invites')).toBeNull()
  })

  it('a failed read shows an error notice instead of an empty space', async () => {
    read.mockRejectedValue(new ApiError(500, 'x', JSON.stringify({ code: 'tailscale_auth_failed' })))
    mount()
    expect(await screen.findByTestId('invite-load-error')).toHaveTextContent('Tailscale did not accept this key')
    expect(screen.queryByTestId('tailscale-invites')).toBeNull()
  })
})

describe('invite helpers', () => {
  it('parses a pasted address list', () => {
    expect(parseEmails('a@x.io, b@x.io\nA@x.io;  c@x.io')).toEqual(['a@x.io', 'b@x.io', 'c@x.io'])
  })
  it('accepts only API access keys', () => {
    expect(looksLikeApiKey(KEY)).toBe(true)
    expect(looksLikeApiKey('tskey-auth-abc-def')).toBe(false)
  })
  it('has a sentence for every refusal code and a fallback', () => {
    expect(inviteErrorText('tailscale_rate_limited')).toMatch(/slow down/)
    expect(inviteErrorText('something_new')).toBe('That did not work. Try again.')
  })
})

describe('invite submission ownership', () => {
  it.each(['success', 'failure'] as const)('locks the credential until save %s', async outcome => {
    read.mockResolvedValue(status({ connected: false }))
    let resolve!: (value: TailscaleInvitesStatus) => void
    let reject!: (error: Error) => void
    connect.mockImplementation(() => new Promise((yes, no) => { resolve = yes; reject = no }))
    const user = userEvent.setup()
    mount()
    const input = await screen.findByTestId('invite-key-input')
    fireEvent.change(input, { target: { value: KEY } })
    fireEvent.click(screen.getByTestId('invite-key-connect'))
    await waitFor(() => expect(input).toBeDisabled())
    await user.type(input, 'newer-key')
    expect(input).toHaveValue(KEY)
    await act(async () => {
      if (outcome === 'success') resolve(status())
      else reject(new Error('save failed'))
    })
    if (outcome === 'success') expect(await screen.findByTestId('invite-connected')).toBeInTheDocument()
    else {
      await screen.findByTestId('invite-key-error')
      expect(input).toBeEnabled()
      expect(input).toHaveValue(KEY)
    }
  })

  it.each(['email', 'link'] as const)('locks the %s draft and deduplicates submits across the access read and create', async mode => {
    read.mockResolvedValue(status())
    let allow!: (value: null) => void
    accessRead.mockImplementation(() => new Promise(resolve => { allow = resolve }))
    let finish!: (value: { invites: TailscaleInvite[] }) => void
    create.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    vi.mocked(api.tailscaleUploadAvatar).mockResolvedValue({ avatar_id: 'first' })
    const user = userEvent.setup()
    mount()
    const emails = await screen.findByTestId('invite-emails')
    fireEvent.change(emails, { target: { value: 'ann@example.com' } })
    const alias = screen.getByTestId('invite-alias')
    fireEvent.change(alias, { target: { value: 'Ann' } })
    const picture = screen.getByTestId('invite-avatar-input')
    await user.upload(picture, new File(['png'], 'first.png', { type: 'image/png' }))
    await screen.findByTestId('invite-avatar-preview')
    if (mode === 'link') fireEvent.click(screen.getByTestId('invite-mode-link'))
    const form = screen.getByTestId('invite-form')
    act(() => { fireEvent.submit(form); fireEvent.submit(form) })
    await waitFor(() => expect(accessRead).toHaveBeenCalledTimes(1))
    for (const id of ['invite-mode-email', 'invite-mode-link', 'invite-alias', 'invite-avatar-input', 'invite-send']) {
      expect(screen.getByTestId(id)).toBeDisabled()
    }
    if (mode === 'email') expect(emails).toBeDisabled()
    else {
      const toggle = screen.getByRole('switch')
      expect(toggle).toHaveAttribute('aria-disabled', 'true')
      fireEvent.click(toggle)
      expect(toggle).toHaveAttribute('aria-checked', 'false')
    }
    expect(create).not.toHaveBeenCalled()
    await act(async () => { allow(null) })
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1))
    await user.type(alias, 'unsent')
    if (mode === 'email') await user.type(emails, ',next@example.com')
    await user.upload(picture, new File(['next'], 'next.png', { type: 'image/png' }))
    fireEvent.submit(form)
    expect(alias).toHaveValue('Ann')
    expect(api.tailscaleUploadAvatar).toHaveBeenCalledTimes(1)
    expect(create).toHaveBeenCalledWith({ emails: mode === 'email' ? ['ann@example.com'] : [], multi_use: false, alias: 'Ann', avatar_id: 'first' })
    await act(async () => { finish({ invites: [invite()] }) })
    await waitFor(() => expect(alias).toBeEnabled())
    expect(alias).toHaveValue('')
    expect(screen.queryByTestId('invite-avatar-preview')).toBeNull()
    fireEvent.click(screen.getByTestId('invite-mode-email'))
    expect(screen.getByTestId('invite-emails')).toHaveValue('')
    expect(create).toHaveBeenCalledTimes(1)
  })

  it('waits for the selected picture before allowing submission', async () => {
    read.mockResolvedValue(status())
    let finish!: (value: { avatar_id: string }) => void
    vi.mocked(api.tailscaleUploadAvatar).mockImplementation(() => new Promise(resolve => { finish = resolve }))
    create.mockResolvedValue({ invites: [invite()] })
    mount()
    fireEvent.change(await screen.findByTestId('invite-emails'), { target: { value: 'ann@example.com' } })
    fireEvent.change(screen.getByTestId('invite-avatar-input'), { target: { files: [new File(['png'], 'one.png', { type: 'image/png' })] } })
    await waitFor(() => expect(api.tailscaleUploadAvatar).toHaveBeenCalledTimes(1))
    expect(screen.getByTestId('invite-avatar-input')).toBeDisabled()
    expect(screen.getByTestId('invite-send')).toBeDisabled()
    fireEvent.submit(screen.getByTestId('invite-form'))
    expect(accessRead).not.toHaveBeenCalled()
    await act(async () => { finish({ avatar_id: 'selected' }) })
    await waitFor(() => expect(screen.getByTestId('invite-send')).toBeEnabled())
    fireEvent.submit(screen.getByTestId('invite-form'))
    await waitFor(() => expect(create).toHaveBeenCalledWith(expect.objectContaining({ avatar_id: 'selected' })))
  })
})

it('keeps the invite draft on cancellation and creation failure, then allows retry', async () => {
  read.mockResolvedValue(status())
  accessRead.mockResolvedValue({ readable: true, waiting: [], approved: [], seeded: [], first_share_ack: false,
    first_share: { needs_ack: true, sessions: 1, memory_items: 1, host: { state: 'available', reason: '', backend: '', laptop: false, laptop_reason: '' } },
  })
  mount()
  fireEvent.change(await screen.findByTestId('invite-emails'), { target: { value: 'ann@example.com' } })
  fireEvent.change(screen.getByTestId('invite-alias'), { target: { value: 'Ann' } })
  fireEvent.submit(screen.getByTestId('invite-form'))
  fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
  await waitFor(() => expect(screen.getByTestId('invite-send')).toBeEnabled())
  expect(create).not.toHaveBeenCalled()
  expect(screen.getByTestId('invite-alias')).toHaveValue('Ann')
  accessRead.mockResolvedValue(null)
  create.mockRejectedValue(new Error('create failed'))
  fireEvent.submit(screen.getByTestId('invite-form'))
  await screen.findByTestId('invite-send-error')
  await waitFor(() => expect(screen.getByTestId('invite-send')).toBeEnabled())
  expect(screen.getByTestId('invite-emails')).toHaveValue('ann@example.com')
  expect(screen.getByTestId('invite-alias')).toHaveValue('Ann')
  create.mockResolvedValue({ invites: [invite()] })
  fireEvent.submit(screen.getByTestId('invite-form'))
  await waitFor(() => expect(screen.getByTestId('invite-alias')).toHaveValue(''))
  expect(create).toHaveBeenCalledTimes(2)
})

it('locks the invite draft during key removal and restores editing after a failure', async () => {
  read.mockResolvedValue(status())
  let reject!: (error: Error) => void
  disconnect.mockImplementation(() => new Promise((_resolve, no) => { reject = no }))
  mount()
  fireEvent.change(await screen.findByTestId('invite-emails'), { target: { value: 'draft@example.com' } })
  fireEvent.click(screen.getByTestId('invite-disconnect'))
  expect(screen.getByTestId('invite-emails')).toBeDisabled()
  fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Remove key' }))
  await waitFor(() => expect(disconnect).toHaveBeenCalledTimes(1))
  expect(screen.getByTestId('invite-alias')).toBeDisabled()
  expect(screen.getByTestId('invite-avatar-input')).toBeDisabled()
  fireEvent.submit(screen.getByTestId('invite-form'))
  expect(create).not.toHaveBeenCalled()
  await act(async () => { reject(new Error('remove failed')) })
  await screen.findByTestId('invite-disconnect-error')
  await waitFor(() => expect(screen.getByTestId('invite-emails')).toBeEnabled())
  expect(screen.getByTestId('invite-emails')).toHaveValue('draft@example.com')
})
