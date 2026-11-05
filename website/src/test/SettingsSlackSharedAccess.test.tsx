/**
 * Settings ▸ Slack ▸ Share Slack access: the warning is always on the page,
 * turning the switch on goes through a confirm, and the list shows each person's
 * name and refuses what Slack did not confirm.
 *
 * Seams: the four shared-access calls are spied on the real `api` object.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from './helpers'
import { api, type SlackSharedAccessData } from '../api/client'
import { ApiError } from '../api/apiError'
import { SlackSharedAccess } from '../pages/settings/SlackSharedAccess'

afterEach(() => {
  vi.restoreAllMocks()
})

function data(over: Partial<SlackSharedAccessData> = {}): SlackSharedAccessData {
  return { enabled: false, users: [], read_only: false, ...over }
}

function toggle() {
  return screen.getByRole('switch', { name: 'Share Slack access' })
}

describe('Share Slack access', () => {
  it('shows the warning while off, before anything can be turned on', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data())
    renderWithProviders(<SlackSharedAccess />)
    const note = await screen.findByRole('note')
    expect(note).toHaveTextContent('Everyone you add acts as you')
    expect(note).toHaveTextContent('your files, your Git and cloud credentials, and your connected tools')
    expect(note).toHaveTextContent('any Slack thread they are part of')
    expect(note).toHaveTextContent('read your other conversations and saved memory, and change what Kiro remembers')
    expect(note).toHaveTextContent('Only add people you trust.')
    expect(toggle()).toHaveAttribute('aria-checked', 'false')
  })

  it('keeps the warning on the page while on', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({ enabled: true }))
    renderWithProviders(<SlackSharedAccess />)
    expect(await screen.findByRole('note')).toHaveTextContent('Only add people you trust.')
    expect(toggle()).toHaveAttribute('aria-checked', 'true')
  })

  it('turns on only after a confirm that names who gets access, and a cancel changes nothing', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({
      users: [
        { user_id: 'U0FRIEND01', name: 'Ada', confirmed: true, reason: 'ok' },
        { user_id: 'U0FRIEND02', name: '', confirmed: true, reason: 'ok' },
      ],
    }))
    const set = vi.spyOn(api, 'setSlackSharedAccess').mockResolvedValue({ ok: true, enabled: true })
    renderWithProviders(<SlackSharedAccess />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    const dialog = await screen.findByRole('dialog', { name: 'Let 2 people act as you in Slack?' })
    const who = within(dialog).getByRole('list', { name: 'People with access' })
    expect(within(who).getAllByRole('listitem').map(li => li.textContent)).toEqual(['Ada', 'U0FRIEND02'])
    expect(within(dialog).getByRole('note')).toHaveTextContent('Only add people you trust.')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(set).not.toHaveBeenCalled()

    await user.click(toggle())
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Share with 2 people' }))
    await waitFor(() => expect(set).toHaveBeenCalledWith(true))
  })

  it('counts and names only the people Slack confirmed, and says who will not get access', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({
      users: [
        { user_id: 'U0FRIEND01', name: 'Ada', confirmed: true, reason: 'ok' },
        { user_id: 'U0GONE0001', name: 'Grace', confirmed: false, reason: 'deactivated' },
      ],
    }))
    const set = vi.spyOn(api, 'setSlackSharedAccess').mockResolvedValue({ ok: true, enabled: true })
    renderWithProviders(<SlackSharedAccess />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    const dialog = await screen.findByRole('dialog', { name: 'Let 1 person act as you in Slack?' })
    const who = within(dialog).getByRole('list', { name: 'People with access' })
    expect(within(who).getAllByRole('listitem').map(li => li.textContent)).toEqual(['Ada'])
    expect(within(dialog).getByText("Grace won't get access. This account is deactivated.")).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Share with 1 person' }))
    await waitFor(() => expect(set).toHaveBeenCalledWith(true))
  })

  it('does not turn on when Slack confirmed nobody on the list', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({
      users: [{ user_id: 'U0GONE0001', name: 'Grace', confirmed: false, reason: 'deactivated' }],
    }))
    const set = vi.spyOn(api, 'setSlackSharedAccess').mockResolvedValue({ ok: true, enabled: true })
    renderWithProviders(<SlackSharedAccess />)
    await userEvent.setup().click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    expect(await screen.findByText(/Slack hasn't confirmed anyone on the list\./)).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(set).not.toHaveBeenCalled()
  })

  it('does not turn on with nobody listed, and says to add someone first', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data())
    const set = vi.spyOn(api, 'setSlackSharedAccess').mockResolvedValue({ ok: true, enabled: true })
    renderWithProviders(<SlackSharedAccess />)
    await userEvent.setup().click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    expect(await screen.findByText('Add someone below first, then turn this on.')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(set).not.toHaveBeenCalled()
    expect(screen.getByText(/open the person's Slack profile, select ⋮, then Copy member ID/)).toBeInTheDocument()
  })

  it('turns off with no confirm', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({ enabled: true }))
    const set = vi.spyOn(api, 'setSlackSharedAccess').mockResolvedValue({ ok: true, enabled: false })
    renderWithProviders(<SlackSharedAccess />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    await waitFor(() => expect(set).toHaveBeenCalledWith(false))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('lists each person by name and says when Slack did not confirm them', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({
      enabled: true,
      users: [
        { user_id: 'U0FRIEND01', name: 'Ada', confirmed: true, reason: 'ok' },
        { user_id: 'U0GONE0001', name: '', confirmed: false, reason: 'deactivated' },
      ],
    }))
    const remove = vi.spyOn(api, 'removeSlackSharedUser').mockResolvedValue({ ok: true })
    renderWithProviders(<SlackSharedAccess />)
    expect(await screen.findByText('Ada')).toBeInTheDocument()
    expect(screen.getByText('U0FRIEND01')).toBeInTheDocument()
    expect(screen.getByText(/Not confirmed by Slack, so they can't use this Crew from Slack\. This account is deactivated\./)).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Remove Ada' }))
    await waitFor(() => expect(remove).toHaveBeenCalledWith('U0FRIEND01'))
  })

  it('refuses a malformed ID without asking the server, and shows a server refusal', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({ enabled: true }))
    const add = vi.spyOn(api, 'addSlackSharedUser').mockRejectedValue(
      new ApiError(400, 'refused', JSON.stringify({ error: 'refused', code: 'bot_user' })),
    )
    renderWithProviders(<SlackSharedAccess />)
    const user = userEvent.setup()
    const input = await screen.findByRole('textbox', { name: 'Slack member ID to add' })
    await user.type(input, 'B0BOT')
    await user.click(screen.getByRole('button', { name: /Add/ }))
    expect(await screen.findByText('Use a Slack member ID that starts with U or W.')).toBeInTheDocument()
    // An input hint, not a failed operation.
    expect(screen.queryByRole('alert')).toBeNull()
    expect(add).not.toHaveBeenCalled()

    await user.clear(input)
    await user.type(input, 'U0BOTUSER1')
    await user.click(screen.getByRole('button', { name: /Add/ }))
    expect(await screen.findByText('This is an app account, not a person.')).toBeInTheDocument()
    expect(add).toHaveBeenCalledWith('U0BOTUSER1')
  })

  it('says when a change was saved but the gateway has not applied it', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({ enabled: true }))
    vi.spyOn(api, 'setSlackSharedAccess').mockRejectedValue(
      new ApiError(409, 'not applied', JSON.stringify({ error: 'not applied', code: 'not_applied' })),
    )
    renderWithProviders(<SlackSharedAccess />)
    await userEvent.setup().click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    expect(await screen.findByText(/Saved, but not applied yet\./)).toBeInTheDocument()
  })

  it('is read-only on a remote session', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({ read_only: true }))
    renderWithProviders(<SlackSharedAccess />)
    expect(await screen.findByText('To change this, open Settings → Messaging Channels → Slack on the computer running Kiro Crew.')).toBeInTheDocument()
    expect(toggle()).toHaveAttribute('aria-disabled', 'true')
    expect(screen.queryByRole('textbox')).toBeNull()
  })
})
