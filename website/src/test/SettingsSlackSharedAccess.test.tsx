/**
 * Settings ▸ Slack ▸ Share Slack access: the warning is always on the page,
 * turning the switch on goes through a confirm, and the list shows each person's
 * name and refuses what Slack did not confirm.
 *
 * Seams: the four shared-access calls are spied on the real `api` object.
 */
import { describe, it, expect, vi, afterEach, onTestFinished } from 'vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
import { focusManager } from '@tanstack/react-query'
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
    expect(note).toHaveTextContent("read your other conversations and this Crew's saved memory, and change what this Crew remembers")
    expect(note).toHaveTextContent("They can't start runs or schedules by typing a command.")
    expect(note).toHaveTextContent('it can still create schedules and start tasks as you, and a schedule it creates keeps running as you after you remove that person')
    // The fix sits next to the risk: the schedules page, by its own nav label.
    expect(note).toHaveTextContent('To stop one, open Schedule and delete it.')
    // Named, never linked: a navigation from here would drop the unsaved Slack form.
    expect(within(note).getByText('Schedule', { selector: 'strong' })).toBeInTheDocument()
    expect(within(note).queryByRole('link')).toBeNull()
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
    const warning = within(dialog).getByRole('note')
    expect(within(warning).getByText('Schedule', { selector: 'strong' })).toBeInTheDocument()
    expect(within(warning).queryByRole('link')).toBeNull()
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(set).not.toHaveBeenCalled()

    await user.click(toggle())
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Let 2 people act as you' }))
    await waitFor(() => expect(set).toHaveBeenCalledWith(true))
  })

  it('does not turn on while anyone listed is unconfirmed, and names who and what to do', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({
      users: [
        { user_id: 'U0FRIEND01', name: 'Ada', confirmed: true, reason: 'ok' },
        { user_id: 'U0GONE0001', name: 'Grace', confirmed: false, reason: 'deactivated' },
        { user_id: 'U0GONE0002', name: '', confirmed: false, reason: 'deactivated' },
      ],
    }))
    const set = vi.spyOn(api, 'setSlackSharedAccess').mockResolvedValue({ ok: true, enabled: true })
    renderWithProviders(<SlackSharedAccess />)
    await userEvent.setup().click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    // Refused at the switch, by the same names the rows show: no confirm opens.
    expect(await screen.findByText("You can't turn sharing on while Slack hasn't verified someone on the list. Remove Grace and U0GONE0002, or fix what each row says.")).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(set).not.toHaveBeenCalled()
  })

  it('does not turn on when Slack confirmed nobody on the list', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({
      users: [{ user_id: 'U0GONE0001', name: 'Grace', confirmed: false, reason: 'deactivated' }],
    }))
    const set = vi.spyOn(api, 'setSlackSharedAccess').mockResolvedValue({ ok: true, enabled: true })
    renderWithProviders(<SlackSharedAccess />)
    await userEvent.setup().click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    expect(await screen.findByText("Slack hasn't verified anyone on the list. Remove them below, or ask them to reactivate their Slack account if it is deactivated; each row says why Slack hasn't verified it.")).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(set).not.toHaveBeenCalled()
  })

  it('does not trust a focus refetch that left before the confirm was answered', async () => {
    const ada = { user_id: 'U0FRIEND01', name: 'Ada', confirmed: true, reason: 'ok' }
    // The focus refetch is in flight with the old answer when the owner clicks; by then
    // the server lists Grace too.
    let answerFocus: (d: SlackSharedAccessData) => void = () => {}
    const get = vi.spyOn(api, 'getSlackSharedAccess')
      .mockResolvedValueOnce(data({ users: [ada] }))
      .mockImplementationOnce(() => new Promise(resolve => { answerFocus = resolve }))
      .mockResolvedValue(data({ users: [ada, { user_id: 'U0GONE0001', name: 'Grace', confirmed: false, reason: 'deactivated' }] }))
    const set = vi.spyOn(api, 'setSlackSharedAccess').mockResolvedValue({ ok: true, enabled: true })
    renderWithProviders(<SlackSharedAccess />)
    onTestFinished(() => { focusManager.setFocused(undefined) })
    const user = userEvent.setup()
    await user.click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    const dialog = await screen.findByRole('dialog', { name: 'Let 1 person act as you in Slack?' })
    act(() => { focusManager.setFocused(false); focusManager.setFocused(true) })
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))
    await user.click(within(dialog).getByRole('button', { name: 'Let 1 person act as you' }))
    act(() => { answerFocus(data({ users: [ada] })) })
    expect(await screen.findByText('The list changed while the confirm was open, so sharing stayed off. Turn it on again to see who gets access.')).toBeInTheDocument()
    expect(get).toHaveBeenCalledTimes(3)
    expect(set).not.toHaveBeenCalled()
  })

  it('holds the switch, Add and Remove while it reads the list again, and sends one change', async () => {
    const ada = { user_id: 'U0FRIEND01', name: 'Ada', confirmed: true, reason: 'ok' }
    let answer: (d: SlackSharedAccessData) => void = () => {}
    vi.spyOn(api, 'getSlackSharedAccess')
      .mockResolvedValueOnce(data({ users: [ada] }))
      .mockImplementationOnce(() => new Promise(resolve => { answer = resolve }))
      .mockResolvedValue(data({ enabled: true, users: [ada] }))
    const set = vi.spyOn(api, 'setSlackSharedAccess').mockResolvedValue({ ok: true, enabled: true })
    const add = vi.spyOn(api, 'addSlackSharedUser').mockResolvedValue({ ok: true, user: ada })
    renderWithProviders(<SlackSharedAccess />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Let 1 person act as you' }))
    // The read is still out: nothing that could change the answer can start. The switch
    // says it is busy and keeps its tab stop.
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-disabled', 'true'))
    expect(toggle()).toHaveAttribute('aria-busy', 'true')
    expect(toggle()).toHaveAttribute('tabindex', '0')
    expect(screen.getByRole('button', { name: 'Remove Ada' })).toBeDisabled()
    await user.type(screen.getByRole('textbox', { name: 'Email or Slack member ID to add' }), 'U0NEWPERS1{Enter}')
    expect(add).not.toHaveBeenCalled()
    // Once the answered confirm has animated out, a second press opens no other.
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await user.click(toggle())
    expect(screen.queryByRole('dialog')).toBeNull()
    act(() => { answer(data({ users: [ada] })) })
    await waitFor(() => expect(set).toHaveBeenCalledWith(true))
    expect(set).toHaveBeenCalledTimes(1)
  })

  it('writes nothing when another window turned sharing on while the confirm was open', async () => {
    const ada = { user_id: 'U0FRIEND01', name: 'Ada', confirmed: true, reason: 'ok' }
    vi.spyOn(api, 'getSlackSharedAccess')
      .mockResolvedValueOnce(data({ users: [ada] }))
      .mockResolvedValue(data({ enabled: true, users: [ada] }))
    const set = vi.spyOn(api, 'setSlackSharedAccess').mockResolvedValue({ ok: true, enabled: true })
    renderWithProviders(<SlackSharedAccess />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Let 1 person act as you' }))
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-checked', 'true'))
    await waitFor(() => expect(toggle()).not.toHaveAttribute('aria-busy'))
    expect(set).not.toHaveBeenCalled()
  })

  it('turns nothing on when the page is left while the list is read again', async () => {
    const ada = { user_id: 'U0FRIEND01', name: 'Ada', confirmed: true, reason: 'ok' }
    let answer: (d: SlackSharedAccessData) => void = () => {}
    const get = vi.spyOn(api, 'getSlackSharedAccess')
      .mockResolvedValueOnce(data({ users: [ada] }))
      .mockImplementationOnce(() => new Promise(resolve => { answer = resolve }))
    const set = vi.spyOn(api, 'setSlackSharedAccess').mockResolvedValue({ ok: true, enabled: true })
    const { unmount } = renderWithProviders(<SlackSharedAccess />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Let 1 person act as you' }))
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))
    unmount()
    await act(async () => { answer(data({ users: [ada] })) })
    expect(set).not.toHaveBeenCalled()
  })

  it('reads the list again after the confirm, so a change the page has not shown yet still stops it', async () => {
    const ada = { user_id: 'U0FRIEND01', name: 'Ada', confirmed: true, reason: 'ok' }
    // The page shows Ada alone; the server already lists Grace too (another window added her).
    const get = vi.spyOn(api, 'getSlackSharedAccess')
      .mockResolvedValueOnce(data({ users: [ada] }))
      .mockResolvedValue(data({ users: [ada, { user_id: 'U0GONE0001', name: 'Grace', confirmed: false, reason: 'deactivated' }] }))
    const set = vi.spyOn(api, 'setSlackSharedAccess').mockResolvedValue({ ok: true, enabled: true })
    // The app's cache never goes stale on its own (api/queryClient.ts): the read must still go out.
    renderWithProviders(<SlackSharedAccess />, { queryDefaults: { staleTime: Infinity } })
    const user = userEvent.setup()
    await user.click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    const dialog = await screen.findByRole('dialog', { name: 'Let 1 person act as you in Slack?' })
    expect(get).toHaveBeenCalledTimes(1)
    await user.click(within(dialog).getByRole('button', { name: 'Let 1 person act as you' }))
    expect(await screen.findByText(/^The list changed while the confirm was open/)).toBeInTheDocument()
    expect(get).toHaveBeenCalledTimes(2)
    expect(set).not.toHaveBeenCalled()
  })

  it('sends nothing when the list cannot be read again after the confirm', async () => {
    const ada = { user_id: 'U0FRIEND01', name: 'Ada', confirmed: true, reason: 'ok' }
    vi.spyOn(api, 'getSlackSharedAccess')
      .mockResolvedValueOnce(data({ users: [ada] }))
      .mockRejectedValue(new ApiError(500, 'unavailable', '{}'))
    const set = vi.spyOn(api, 'setSlackSharedAccess').mockResolvedValue({ ok: true, enabled: true })
    renderWithProviders(<SlackSharedAccess />, { queryDefaults: { staleTime: Infinity } })
    const user = userEvent.setup()
    await user.click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Let 1 person act as you' }))
    // The failed read is shown as the section's load failure, with Try again.
    expect(await screen.findByText('Could not load the shared access settings.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try Again' })).toBeInTheDocument()
    expect(set).not.toHaveBeenCalled()
  })

  it("stops when the owner's own ID on the list stops being theirs while the confirm is open", async () => {
    const ada = { user_id: 'U0FRIEND01', name: 'Ada', confirmed: true, reason: 'ok' }
    // Same IDs, same unconfirmed flag: only the reason changes, from the owner's own ID
    // (no access needed) to someone else Slack did not confirm.
    vi.spyOn(api, 'getSlackSharedAccess')
      .mockResolvedValueOnce(data({ users: [ada, { user_id: 'U0OWNER001', name: '', confirmed: false, reason: 'owner' }] }))
      .mockResolvedValue(data({ users: [ada, { user_id: 'U0OWNER001', name: '', confirmed: false, reason: 'deactivated' }] }))
    const set = vi.spyOn(api, 'setSlackSharedAccess').mockResolvedValue({ ok: true, enabled: true })
    renderWithProviders(<SlackSharedAccess />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Let 1 person act as you' }))
    expect(await screen.findByText(/^The list changed while the confirm was open/)).toBeInTheDocument()
    expect(set).not.toHaveBeenCalled()
  })

  it('drops the nobody-confirmed hint once the last person is removed', async () => {
    const grace = { user_id: 'U0GONE0001', name: 'Grace', confirmed: false, reason: 'deactivated' }
    vi.spyOn(api, 'getSlackSharedAccess')
      .mockResolvedValueOnce(data({ users: [grace] }))
      .mockResolvedValue(data())
    vi.spyOn(api, 'removeSlackSharedUser').mockResolvedValue({ ok: true })
    renderWithProviders(<SlackSharedAccess />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    expect(await screen.findByText(/^Slack hasn't verified anyone on the list/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Remove Grace' }))
    await waitFor(() => expect(screen.queryByText('Grace')).toBeNull())
    expect(screen.queryByText(/^Slack hasn't verified anyone on the list/)).toBeNull()
  })

  it('drops the pending hint once the person it names is removed', async () => {
    const ada = { user_id: 'U0FRIEND01', name: 'Ada', confirmed: true, reason: 'ok' }
    const grace = { user_id: 'U0GONE0001', name: 'Grace', confirmed: false, reason: 'deactivated' }
    vi.spyOn(api, 'getSlackSharedAccess')
      .mockResolvedValueOnce(data({ users: [ada, grace] }))
      .mockResolvedValue(data({ users: [ada] }))
    vi.spyOn(api, 'removeSlackSharedUser').mockResolvedValue({ ok: true })
    renderWithProviders(<SlackSharedAccess />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    expect(await screen.findByText(/^You can't turn sharing on while/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Remove Grace' }))
    await waitFor(() => expect(screen.queryByText('Grace')).toBeNull())
    expect(screen.queryByText(/^You can't turn sharing on while/)).toBeNull()
  })

  it("treats a list holding only the owner's own ID as nobody listed", async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({
      users: [{ user_id: 'U0OWNER001', name: '', confirmed: false, reason: 'owner' }],
    }))
    renderWithProviders(<SlackSharedAccess />)
    await userEvent.setup().click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    expect(await screen.findByText('Add someone below first, then turn this on.')).toBeInTheDocument()
    expect(screen.queryByText(/^Slack hasn't verified anyone/)).toBeNull()
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
    expect(screen.getByText(/^This account is deactivated\. They can't use this Crew now\. If Slack says this account is fine when they next message \(it asks again after a minute\), they get access without asking you\. Remove them to keep them out\.$/)).toHaveClass('text-warn-text')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Remove Ada' }))
    await waitFor(() => expect(remove).toHaveBeenCalledWith('U0FRIEND01'))
  })

  it("says the owner's own ID is theirs without saying it can't use the Crew", async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({
      enabled: true,
      users: [{ user_id: 'U0OWNER001', name: '', confirmed: false, reason: 'owner' }],
    }))
    renderWithProviders(<SlackSharedAccess />)
    expect(await screen.findByText(/^This is you\. As the owner you already have access, so remove this row\.$/)).toBeInTheDocument()
    expect(screen.queryByText(/can't use this Crew/)).toBeNull()
  })

  it("lets the owner's own ID on the list neither block turning on nor appear in the confirm", async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({
      users: [
        { user_id: 'U0FRIEND01', name: 'Ada', confirmed: true, reason: 'ok' },
        { user_id: 'U0OWNER001', name: '', confirmed: false, reason: 'owner' },
      ],
    }))
    renderWithProviders(<SlackSharedAccess />)
    await userEvent.setup().click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    const dialog = await screen.findByRole('dialog', { name: 'Let 1 person act as you in Slack?' })
    const who = within(dialog).getByRole('list', { name: 'People with access' })
    expect(within(who).getAllByRole('listitem').map(li => li.textContent)).toEqual(['Ada'])
    expect(within(dialog).queryByText(/U0OWNER001/)).toBeNull()
    expect(screen.queryByText(/^You can't turn sharing on/)).toBeNull()
  })

  it('shows a failed Slack check as an error notice and a Slack verdict as plain text', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({
      enabled: true,
      users: [
        { user_id: 'U0DOWN0001', name: '', confirmed: false, reason: 'unverifiable' },
        { user_id: 'U0GONE0001', name: '', confirmed: false, reason: 'deactivated' },
      ],
    }))
    renderWithProviders(<SlackSharedAccess />)
    await screen.findByText('U0DOWN0001')
    const failed = screen.getByText(/Could not check this ID with Slack\./).closest('[role="status"]')
    expect(failed).not.toBeNull()
    expect(failed).toHaveTextContent(/^Could not check this ID with Slack\. Check that Slack is connected; this page asks Slack again when you reopen it or come back to it, and an answer can be up to a minute old\. They can't use this Crew now\. If Slack says this account is fine when they next message \(it asks again after a minute\), they get access without asking you\. Remove them to keep them out\.$/)
    expect(failed).not.toHaveTextContent(/deactivated/)
    expect(screen.getByText(/This account is deactivated\./).closest('[role="status"]')).toBeNull()
  })

  it('refuses to turn on while Slack could not be asked about someone, and says when it asks again', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({
      users: [
        { user_id: 'U0FRIEND01', name: 'Ada', confirmed: true, reason: 'ok' },
        { user_id: 'U0LIN00001', name: 'Lin', confirmed: false, reason: 'unverifiable' },
      ],
    }))
    const add = vi.spyOn(api, 'addSlackSharedUser')
    renderWithProviders(<SlackSharedAccess />)
    await userEvent.setup().click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    expect(await screen.findByText("You can't turn sharing on while Slack hasn't verified someone on the list. Remove Lin, or fix what each row says.")).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
    // The row says when the page asks Slack again; it offers no button that would re-add the ID.
    expect(screen.getByText(/^Could not check this ID with Slack\. Check that Slack is connected; this page asks Slack again when you reopen it or come back to it, and an answer can be up to a minute old\.$/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /check again/i })).toBeNull()
    expect(add).not.toHaveBeenCalled()
  })

  it('refuses a malformed ID without asking the server, and shows a server refusal', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({ enabled: true }))
    const add = vi.spyOn(api, 'addSlackSharedUser').mockRejectedValue(
      new ApiError(400, 'refused', JSON.stringify({ error: 'refused', code: 'bot_user' })),
    )
    renderWithProviders(<SlackSharedAccess />)
    const user = userEvent.setup()
    const input = await screen.findByRole('textbox', { name: 'Email or Slack member ID to add' })
    await user.type(input, 'B0BOT')
    await user.click(screen.getByRole('button', { name: /Add/ }))
    // The find-ID help comes with the refusal: the empty-list help is gone once anyone is listed.
    expect(await screen.findByText("Enter an email, or a Slack member ID that starts with U or W. To find a member ID, open the person's Slack profile, select ⋮, then Copy member ID.")).toBeInTheDocument()
    // An input hint, not a failed operation.
    expect(screen.queryByRole('alert')).toBeNull()
    expect(add).not.toHaveBeenCalled()

    await user.clear(input)
    await user.type(input, 'U0BOTUSER1')
    await user.click(screen.getByRole('button', { name: /Add/ }))
    expect(await screen.findByText('This is an app account, not a person.')).toBeInTheDocument()
    expect(add).toHaveBeenCalledWith({ user_id: 'U0BOTUSER1' })
  })

  it('sends an email as an email, takes the ID out of a profile link, and refuses other links', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({ enabled: true }))
    const add = vi.spyOn(api, 'addSlackSharedUser').mockRejectedValue(
      new ApiError(400, 'refused', JSON.stringify({ error: 'refused', code: 'email_lookup_unavailable' })),
    )
    renderWithProviders(<SlackSharedAccess />)
    const user = userEvent.setup()
    const input = await screen.findByRole('textbox', { name: 'Email or Slack member ID to add' })
    await user.type(input, ' ada@example.com {Enter}')
    await waitFor(() => expect(add).toHaveBeenCalledWith({ email: 'ada@example.com' }))
    expect(await screen.findByText("This Slack app can't look people up by email. Add the users:read.email bot scope to the app and reinstall it, or enter their member ID instead.")).toBeInTheDocument()

    await user.clear(input)
    await user.type(input, 'https://acme.slack.com/team/U0FRIEND01{Enter}')
    await waitFor(() => expect(add).toHaveBeenLastCalledWith({ user_id: 'U0FRIEND01' }))

    add.mockClear()
    for (const bad of ['https://evil.example/team/U0FRIEND01', 'http://acme.slack.com/team/U0FRIEND01', 'https://acme.slack.com/archives/C0123ABC']) {
      await user.clear(input)
      await user.type(input, `${bad}{Enter}`)
      expect(await screen.findByText(/^Enter an email, or a Slack member ID/)).toBeInTheDocument()
    }
    expect(add).not.toHaveBeenCalled()
  })

  it('says to save the owner ID first when the server has none', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data())
    vi.spyOn(api, 'addSlackSharedUser').mockRejectedValue(
      new ApiError(400, 'refused', JSON.stringify({ error: 'refused', code: 'owner_unset' })),
    )
    renderWithProviders(<SlackSharedAccess />)
    const user = userEvent.setup()
    await user.type(await screen.findByRole('textbox', { name: 'Email or Slack member ID to add' }), 'U0FRIEND01{Enter}')
    expect(await screen.findByText('Save your Owner Slack member ID above first, so this list can tell you apart from the people you share with.')).toBeInTheDocument()
  })

  it('says under the switch that sharing is still on when turning it off was saved but not applied', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({ enabled: true }))
    vi.spyOn(api, 'setSlackSharedAccess').mockRejectedValue(
      new ApiError(409, 'not applied', JSON.stringify({ error: 'not applied', code: 'not_applied' })),
    )
    renderWithProviders(<SlackSharedAccess />)
    await userEvent.setup().click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    // The state in effect first, beside the switch that shows it; why and what to do next.
    const state = await screen.findByText('Sharing is still on: turning it off is saved but not applied yet.')
    // A rejected request, so the error notice (errors-use-error-notice), not a status line.
    const notice = state.closest('[role="alert"]')
    expect(notice).not.toBeNull()
    expect(notice).toHaveTextContent(/^Sharing is still on: turning it off is saved but not applied yet\.Slack still uses the earlier list and switch, so anyone who had access still has it\. Your change is saved in config\.json, but another setting still holds the earlier one, for example the same setting in config\.local\.json\. Check that file; the switch and the list show what is in effect\.$/)
    // Under the switch, above the warning and the list, not at the foot of the section.
    expect(toggle().compareDocumentPosition(notice!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(notice!.compareDocumentPosition(screen.getByRole('note')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getAllByRole('alert')).toHaveLength(1)
  })

  it('says sharing is still off when turning it on was saved but not applied', async () => {
    const ada = { user_id: 'U0FRIEND01', name: 'Ada', confirmed: true, reason: 'ok' }
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({ users: [ada] }))
    vi.spyOn(api, 'setSlackSharedAccess').mockRejectedValue(
      new ApiError(409, 'not applied', JSON.stringify({ error: 'not applied', code: 'not_applied' })),
    )
    renderWithProviders(<SlackSharedAccess />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Let 1 person act as you' }))
    expect((await screen.findByText('Sharing is still off: turning it on is saved but not applied yet.')).closest('[role="alert"]')).not.toBeNull()
  })

  it('names what Kiro Crew could not read when a change was not applied', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({ enabled: true }))
    vi.spyOn(api, 'setSlackSharedAccess').mockRejectedValue(
      new ApiError(409, 'not applied', JSON.stringify({ error: 'not applied', code: 'not_applied', unread: ['slack'] })),
    )
    renderWithProviders(<SlackSharedAccess />)
    await userEvent.setup().click(await screen.findByRole('switch', { name: 'Share Slack access' }))
    // The raw section key stays out of the notice; it says which state is on screen.
    expect(await screen.findByText('Sharing is still on: turning it off is saved but not applied yet.')).toBeInTheDocument()
    expect(screen.getByText("Kiro Crew could not read its Slack settings earlier, so Slack still uses the earlier list and switch, and anyone who had access still has it. Your change is saved; restart Kiro Crew to apply it (first check the slack section of config.local.json in the Kiro Crew data folder). The switch and the list show what is in effect.")).toBeInTheDocument()
  })

  it('names the person in a list change that was saved but not applied', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({
      enabled: true,
      users: [{ user_id: 'U0FRIEND01', name: 'Ada', confirmed: true, reason: 'ok' }],
    }))
    const notApplied = new ApiError(409, 'not applied', JSON.stringify({ error: 'not applied', code: 'not_applied' }))
    vi.spyOn(api, 'removeSlackSharedUser').mockRejectedValue(notApplied)
    vi.spyOn(api, 'addSlackSharedUser').mockRejectedValue(notApplied)
    renderWithProviders(<SlackSharedAccess />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Remove Ada' }))
    expect((await screen.findByText('Ada is still on the list Slack uses: removing them is saved but not applied yet.')).closest('[role="alert"]')).not.toBeNull()
    await user.type(screen.getByRole('textbox', { name: 'Email or Slack member ID to add' }), 'U0NEWPERS1')
    await user.click(screen.getByRole('button', { name: /Add/ }))
    expect((await screen.findByText("U0NEWPERS1 isn't on the list Slack uses yet: adding them is saved but not applied.")).closest('[role="alert"]')).not.toBeNull()
    expect(screen.queryByText(/^Ada is still on the list Slack uses/)).toBeNull()
  })

  it('says at the switch that nobody has access while off, and titles the list for that', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({
      users: [{ user_id: 'U0FRIEND01', name: 'Ada', confirmed: true, reason: 'ok' }],
    }))
    renderWithProviders(<SlackSharedAccess />)
    expect(await screen.findByText('Off: no one below can use this Crew from Slack.')).toBeInTheDocument()
    expect(screen.getByText('People to share with')).toBeInTheDocument()
    expect(screen.queryByText('People with access')).toBeNull()
    // The list keeps its config-key anchor in either state.
    expect(document.querySelector('[data-setting-key="slack.shared_access.user_ids"]')).toHaveAttribute('data-setting-label', 'People to share with')
  })

  it('says nothing about later access in a row while sharing is off', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({
      users: [{ user_id: 'U0GONE0001', name: 'Grace', confirmed: false, reason: 'deactivated' }],
    }))
    renderWithProviders(<SlackSharedAccess />)
    expect(await screen.findByText(/^This account is deactivated\.$/)).toBeInTheDocument()
    expect(screen.queryByText(/get access without asking you/)).toBeNull()
  })

  it('offers Try again when the section could not load, and loads it', async () => {
    const get = vi.spyOn(api, 'getSlackSharedAccess')
      .mockRejectedValueOnce(new ApiError(500, 'unavailable', '{}'))
      .mockResolvedValue(data({ enabled: true }))
    renderWithProviders(<SlackSharedAccess />)
    expect(await screen.findByText('Could not load the shared access settings.')).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Try Again' }))
    expect(await screen.findByRole('switch', { name: 'Share Slack access' })).toBeInTheDocument()
    expect(get).toHaveBeenCalledTimes(2)
  })

  it('titles the list People with access only while on, and says it is on', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({
      enabled: true,
      users: [{ user_id: 'U0FRIEND01', name: 'Ada', confirmed: true, reason: 'ok' }],
    }))
    renderWithProviders(<SlackSharedAccess />)
    expect(await screen.findByText('People with access')).toBeInTheDocument()
    expect(screen.queryByText('People to share with')).toBeNull()
    expect(screen.queryByText(/^Off: no one below/)).toBeNull()
    // The switch's state in words too, not only its colour.
    expect(screen.getByText('On: the people below that Slack verified can use this Crew from Slack.')).toBeInTheDocument()
    expect(document.querySelector('[data-setting-key="slack.shared_access.user_ids"]')).toHaveAttribute('data-setting-label', 'People with access')
  })

  it('names what only the owner can do in plain words, except on a remote session', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({ enabled: true }))
    const { unmount } = renderWithProviders(<SlackSharedAccess />)
    expect(await screen.findByText('Only you can change this list or switch, approve tool prompts for a whole session, or use Slack Code channels.')).toBeInTheDocument()
    unmount()
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({ enabled: true, read_only: true }))
    renderWithProviders(<SlackSharedAccess />)
    await screen.findByText(/^To change this, open Settings/)
    expect(screen.queryByText(/^Only you can change this list/)).toBeNull()
  })

  it('tells a remote viewer nothing about removing someone it cannot remove', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({
      enabled: true,
      read_only: true,
      users: [{ user_id: 'U0GONE0001', name: 'Grace', confirmed: false, reason: 'deactivated' }],
    }))
    renderWithProviders(<SlackSharedAccess />)
    expect(await screen.findByText(/^This account is deactivated\. They can't use this Crew now\. If Slack says this account is fine when they next message \(it asks again after a minute\), they get access without asking you\.$/)).toBeInTheDocument()
    expect(screen.queryByText(/Remove them to keep them out/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Remove Grace' })).toBeNull()
  })

  it('is read-only on a remote session', async () => {
    vi.spyOn(api, 'getSlackSharedAccess').mockResolvedValue(data({ read_only: true }))
    renderWithProviders(<SlackSharedAccess />)
    expect(await screen.findByText('To change this, open Settings → Messaging Channels → Slack on the computer where Kiro Crew is installed (not this remote session).')).toBeInTheDocument()
    expect(toggle()).toHaveAttribute('aria-disabled', 'true')
    expect(screen.queryByRole('textbox')).toBeNull()
  })
})

// The section is its own chunk inside Settings ▸ Slack (SlackPanel.tsx, lazyRetry
// `inline`). This pins what the Slack panel adds: a failed section load leaves the rest
// of the form mounted with what was typed, the raw chunk error is never printed, and
// Try Again then shows the section.
describe('Share Slack access inside the Slack panel', () => {
  const chunk = { fail: true }

  afterEach(() => {
    vi.doUnmock('../pages/settings/SlackSharedAccess')
    vi.resetModules()
    vi.unstubAllGlobals()
  })

  it('keeps the Slack form and its typed values when the section fails to load, and Try Again loads it', async () => {
    chunk.fail = true
    vi.resetModules()
    vi.doMock('../pages/settings/SlackSharedAccess', async () => {
      if (chunk.fail) throw new TypeError('Importing a module script failed.')
      return await vi.importActual('../pages/settings/SlackSharedAccess')
    })
    // The restart badge fetches the config schema; answer it so its notice stays quiet.
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ entries: [] }) })))
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const { api: liveApi } = await import('../api/client')
    vi.spyOn(liveApi, 'getSlackConfig').mockResolvedValue({
      connected: false, connect_error: '', configured: true, read_only: false,
      bot_token_set: false, app_token_set: false, bot_token_preview: '', app_token_preview: '',
      owner_id: 'U000OWNER', command: 'kirocrew', allowed_enterprise_ids: [],
      reactions_enabled: false, show_thinking: false,
    })
    vi.spyOn(liveApi, 'getSlackManifest').mockResolvedValue({
      alias: 'tester', manifest: 'display_information:\n  name: tester\n', create_url: 'https://example.invalid/apps/new',
    })
    vi.spyOn(liveApi, 'getSlackSharedAccess').mockResolvedValue(data())
    const { SlackPanel } = await import('../pages/settings/SlackPanel')
    renderWithProviders(<SlackPanel />)

    const command = await screen.findByPlaceholderText('kirocrew')
    await userEvent.setup().type(command, '-typed')
    const tryAgain = await screen.findByRole('button', { name: 'Try Again' }, { timeout: 5000 })
    expect(screen.getByText('Could not load the shared access settings.')).toBeInTheDocument()
    // Read out to assistive tech, never printed: a chunk URL means nothing to the owner.
    const failure = screen.getByText('Could not load the shared access settings.').closest('[role="alert"]')
    expect(failure?.querySelector('strong + span')).toHaveClass('sr-only')
    expect(screen.getByPlaceholderText('kirocrew'), 'the form stays mounted').toHaveValue('kirocrew-typed')

    chunk.fail = false
    await userEvent.setup().click(tryAgain)
    expect(await screen.findByRole('switch', { name: 'Share Slack access' }, { timeout: 5000 })).toBeInTheDocument()
    expect(screen.getByPlaceholderText('kirocrew')).toHaveValue('kirocrew-typed')
  })
})
