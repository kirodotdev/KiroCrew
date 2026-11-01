import { describe, it, expect, vi, beforeEach, beforeAll, afterEach } from 'vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ChannelPage from '../pages/ChannelPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { ApiError } from '../api/apiError'

vi.mock('../api/client')

beforeAll(() => {
  // jsdom doesn't implement scrollIntoView
  Element.prototype.scrollIntoView = vi.fn()
})

const mockChannel = {
  id: 'ch1',
  topic: 'Test Channel',
  members: {
    a1: { id: 'a1', role: 'Researcher', agent_name: 'kirocrew', state: 'listening', listen_mode: 'mention', approval_policy: 'writes', session_key: 'k1' },
  },
  messages: [],
}

/** A second channel, so `channels[0]` has somewhere wrong to fall back TO. */
const mockChannel2 = {
  id: 'ch2',
  topic: 'Second Channel',
  members: {
    b1: { id: 'b1', role: 'Scribe', agent_name: 'kirocrew', state: 'listening', listen_mode: 'mention', approval_policy: 'writes', session_key: 'k2' },
  },
  messages: [],
}

describe('ChannelPage — Clear Context', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [mockChannel] })
    vi.mocked(api).channelGet = vi.fn().mockResolvedValue(mockChannel)
    vi.mocked(api).channelPresets = vi.fn().mockResolvedValue({ presets: [] })
    vi.mocked(api).channelClearContext = vi.fn().mockResolvedValue({ ok: true, cleared: ['Researcher'] })
  })

  afterEach(() => { vi.useRealTimers() })

  it('renders Clear Context button in channel header', async () => {
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())
  })

  /** Opens the clear-all dialog and confirms via the button that RESTATES the act. */
  const confirmClearAll = async () => {
    await userEvent.click(screen.getByTitle('Clear all context'))
    await waitFor(() => screen.getByRole('button', { name: 'Clear context and delete messages' }))
    await userEvent.click(screen.getByRole('button', { name: 'Clear context and delete messages' }))
  }

  /** Answers the per-agent in-page confirm; its button restates the act, like the row's. */
  const answerAgentClear = async (yes = true) => {
    const dialog = await screen.findByRole('dialog')
    await userEvent.click(within(dialog).getByRole('button', {
      name: yes ? 'Clear context' : 'Cancel',
    }))
  }

  /** Fire one `kirocrew-channel` window event, the page's stand-in for the socket. */
  const wsEvent = (type: string, data: unknown) => {
    act(() => {
      window.dispatchEvent(new CustomEvent('kirocrew-channel', { detail: { type, data } }))
    })
  }

  it('withdraws the clear-all confirmation when its channel disappears under it', async () => {
    // `channel` is recomputed every render as `find(activeId) || channels[0]`, so a channel
    // closed by ANOTHER client while this dialog is open used to leave the dialog standing and
    // its confirm pointed at the fallback channel. The dialog now resolves the id captured at
    // open, so it unmounts instead of retargeting.
    vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [mockChannel, mockChannel2] })
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())
    await userEvent.click(screen.getByTitle('Clear all context'))
    await waitFor(() => screen.getByRole('button', { name: 'Clear context and delete messages' }))

    wsEvent('channel_closed', { channel_id: 'ch1' })

    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: 'Clear context and delete messages' }),
        'the confirm button outlived the channel it was opened for',
      ).toBeNull(),
    )
  })

  it('never retargets the clear-all onto a channel the user did not choose', async () => {
    // The consequence test for the same defect: confirming after ch1 closed must not clear ch2.
    vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [mockChannel, mockChannel2] })
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())
    await userEvent.click(screen.getByTitle('Clear all context'))
    await waitFor(() => screen.getByRole('button', { name: 'Clear context and delete messages' }))

    wsEvent('channel_closed', { channel_id: 'ch1' })
    const stillThere = screen.queryByRole('button', { name: 'Clear context and delete messages' })
    if (stillThere) await userEvent.click(stillThere)

    expect(vi.mocked(api).channelClearContext).not.toHaveBeenCalledWith('ch2', 'all')
  })

  it('still renders a clear failure when activeId names a channel that is gone', async () => {
    // `shownClearError` was keyed on `activeId`. The divergence has to be MADE, not assumed:
    // ch1 is removed by another client's `channel_closed` while `activeId` stays ch1, so
    // `channel` falls back to ch2 and the ch2-scoped failure matched nothing. An earlier
    // version of this test skipped the close and passed on the unfixed page -- vacuously.
    vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [mockChannel, mockChannel2] })
    vi.mocked(api).channelClearContext = vi.fn().mockRejectedValue(new Error('boom'))
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())

    // The divergence comes FIRST: ch1 leaves `channels` while `activeId` still names it, so
    // `channel` falls back to ch2 and the clear below is issued against ch2 -- the state
    // Opus F2 describes, where the ch2-scoped failure was compared against activeId=ch1.
    wsEvent('channel_closed', { channel_id: 'ch1' })
    await confirmClearAll()

    await waitFor(() =>
      expect(
        screen.getByText(/failed to clear context/i),
        'a destructive action failed with no notice anywhere',
      ).toBeInTheDocument(),
    )
  })

  it('keeps a presets-load failure when the first channel is auto-selected', async () => {
    // `reload` assigns `activeId` from null AFTER the presets read rejects, and the reset effect
    // used to fire an unconditional `setError(null)` on that transition, erasing the notice and
    // leaving FALLBACK_PRESETS on screen unreported.
    vi.mocked(api).channelPresets = vi.fn().mockRejectedValue(new Error('presets down'))
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())

    expect(
      await screen.findByText(/failed to load team presets/i),
      'the presets failure was erased by the first channel selection',
    ).toBeInTheDocument()
  })

  it('still ASKS before a per-agent clear, and does nothing when that is declined', async () => {
    const spy = vi.spyOn(window, 'confirm')
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByRole('button', { name: /^\d+ agents?$/ }))
    await userEvent.click(screen.getByRole('button', { name: /^\d+ agents?$/ }))
    await waitFor(() => screen.getByTitle('Clear context'))
    await userEvent.click(screen.getByTitle('Clear context'))

    const dialog = await screen.findByRole('dialog')
    expect(dialog.textContent, 'the in-page confirm lost the role it resets').toContain('Researcher')
    expect(spy, 'the per-agent clear fell back to the native confirm').not.toHaveBeenCalled()
    expect(
      screen.getByTitle('Clear context').getAttribute('aria-busy'),
      'the row spins behind the confirm, as if the clear were already running',
    ).not.toBe('true')
    await answerAgentClear(false)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(
      vi.mocked(api).channelClearContext,
      'a declined per-agent clear ran anyway',
    ).not.toHaveBeenCalled()
  })

  it('withdraws the per-agent confirmation when its agent leaves under it', async () => {
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByRole('button', { name: '1 agent' }))
    await userEvent.click(screen.getByRole('button', { name: '1 agent' }))
    await waitFor(() => screen.getByTitle('Clear context'))
    await userEvent.click(screen.getByTitle('Clear context'))
    await screen.findByRole('dialog')

    wsEvent('channel_agent_left', { channel_id: 'ch1', agent_id: 'a1' })

    await waitFor(() =>
      expect(screen.queryByRole('dialog'), 'the confirm outlived the agent it was opened for').toBeNull(),
    )
    expect(vi.mocked(api).channelClearContext).not.toHaveBeenCalled()
  })

  it("a per-agent clear does not erase another agent's unresolved failure", async () => {
    const twoAgents = {
      ...mockChannel,
      members: {
        a1: { ...mockChannel.members.a1 },
        a2: { ...mockChannel.members.a1, id: 'a2', role: 'Scribe', session_key: 'k2' },
      },
    }
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [twoAgents] })
    vi.mocked(api).channelGet = vi.fn().mockResolvedValue(twoAgents)
    vi.mocked(api).channelClearContext = vi.fn().mockImplementation(
      async (_id: string, _scope: string, agentId?: string) => {
        if (agentId === 'a1') throw new Error('a1 clear failed')
        return { ok: true, cleared: ['Scribe'] }
      },
    )
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByRole('button', { name: /^\d+ agents?$/ }))
    await userEvent.click(screen.getByRole('button', { name: /^\d+ agents?$/ }))
    await waitFor(() => expect(screen.getAllByTitle('Clear context')).toHaveLength(2))

    const [first, second] = screen.getAllByTitle('Clear context')
    await userEvent.click(first)
    await answerAgentClear()
    const notice = await screen.findByTestId('clear-context-error')
    expect(notice.textContent).toContain('a1 clear failed')

    await userEvent.click(second)
    await answerAgentClear()
    await waitFor(() => expect(screen.getByTestId('clear-context-done')).toBeInTheDocument())
    expect(
      screen.queryByTestId('clear-context-error'),
      "clearing one agent erased another agent's failure, which nothing retried",
    ).not.toBeNull()
  })

  it('still reports a clear that genuinely FAILED on a channel the user has left', async () => {
    // Opus 5's finding. The off-screen drop was applied to every result, so a confirmed
    // destructive action that then failed outright went unreported anywhere: the scoped
    // notice discarded it and `runClear` swallowed the rejection.
    vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [mockChannel, mockChannel2] })
    vi.mocked(api).channelGet = vi.fn().mockImplementation(
      (id: string) => Promise.resolve(id === 'ch2' ? mockChannel2 : mockChannel),
    )
    let breakIt: (e: unknown) => void = () => {}
    vi.mocked(api).channelClearContext = vi.fn().mockImplementation(
      () => new Promise((_resolve, reject) => { breakIt = reject }),
    )
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())
    await confirmClearAll()

    await userEvent.click(screen.getByText('Second Channel'))
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())

    await act(async () => {
      breakIt(new Error('server error'))
      await Promise.resolve()
    })

    const notice = await screen.findByTestId('channel-error')
    expect(
      notice.textContent,
      'a destructive action that failed was dropped with no notice anywhere',
    ).toContain('Failed to clear context')
  })

  it('does not let a late success on another channel erase this one\'s acknowledgment', async () => {
    vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [mockChannel, mockChannel2] })
    vi.mocked(api).channelGet = vi.fn().mockImplementation(
      (id: string) => Promise.resolve(id === 'ch2' ? mockChannel2 : mockChannel),
    )
    const releases: Array<(v: unknown) => void> = []
    vi.mocked(api).channelClearContext = vi.fn().mockImplementation(
      () => new Promise(resolve => { releases.push(resolve) }),
    )
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())
    await confirmClearAll()                                   // ch1, left in flight

    await userEvent.click(screen.getByText('Second Channel'))
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())
    await confirmClearAll()                                   // ch2, also in flight

    // ch2 settles first and renders its acknowledgment, then ch1's late success arrives.
    await act(async () => { releases[1]?.({ ok: true, cleared: ['Scribe'] }); await Promise.resolve() })
    await waitFor(() => expect(screen.getByTestId('clear-context-done')).toBeInTheDocument())
    await act(async () => { releases[0]?.({ ok: true, cleared: ['Researcher'] }); await Promise.resolve() })

    expect(
      screen.queryByTestId('clear-context-done'),
      "a late success on the channel the user left erased the acknowledgment for the one they are on",
    ).not.toBeNull()
  })

  it('calls channelClearContext with scope=all on confirm', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())
    await confirmClearAll()
    await waitFor(() => expect(vi.mocked(api).channelClearContext).toHaveBeenCalledWith('ch1', 'all'))
  })

  it('acknowledges inside the header, so a success cannot displace the controls', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())
    await confirmClearAll()
    const done = await screen.findByTestId('clear-context-done')
    expect(
      done.parentElement,
      'the acknowledgment left the header, so it displaces the Close control again',
    ).toContainElement(screen.getByTitle('Close channel'))
  })

  it('marks the header button busy while the clear-all request is in flight', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    let release: (v: unknown) => void = () => {}
    vi.mocked(api).channelClearContext = vi.fn().mockImplementation(
      () => new Promise(resolve => { release = resolve })
    )
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())
    const btn = screen.getByTitle('Clear all context')
    await userEvent.click(btn)
    await waitFor(() => screen.getByRole('button', { name: 'Clear context and delete messages' }))
    await userEvent.click(screen.getByRole('button', { name: 'Clear context and delete messages' }))

    // `aria-disabled`, not `disabled`: the dialog's focus trap restores focus to this button on
    // unmount and its FOCUSABLE selector skips `button:not([disabled])`, so a truly disabled
    // button dropped a keyboard user onto `<body>`. The guarantee `disabled` used to give --
    // that a second click cannot re-open the dialog mid-flight -- is asserted directly below,
    // because an aria attribute alone would announce the state without enforcing it.
    await waitFor(() => expect(btn).toHaveAttribute('aria-disabled', 'true'))
    expect(btn).toHaveAttribute('aria-busy', 'true')
    expect(btn, 'a disabled button is unfocusable, which breaks the modal focus restore')
      .not.toBeDisabled()
    expect(btn, 'aria-disabled is invisible on its own: Btn styles only the `disabled:` '
      + 'variants, so the button looked clickable and silently swallowed the click')
      .toHaveClass('aria-disabled:opacity-30', 'aria-disabled:cursor-not-allowed')

    // Awaited, not read synchronously: the shared dialog stays mounted through its exit
    // animation, so an immediate read finds the button it is still animating out.
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Clear context and delete messages' })).toBeNull(),
    )
    await userEvent.click(btn)
    expect(
      screen.queryByRole('button', { name: 'Clear context and delete messages' }),
      'clicking the busy button re-opened the confirm dialog',
    ).toBeNull()

    release({ ok: true, cleared: ['Researcher'] })
    await waitFor(() => expect(btn).not.toHaveAttribute('aria-disabled', 'true'))
  })

  it('promises no busy-agent protection the handler does not provide', async () => {
    // Rewritten, not deleted, so re-adding the clause reddens something. The clause this
    // pins the ABSENCE of used to promise that a working agent's context and the channel's
    // messages survive a clear-all. `api_channel_clear_context` resets every member whose
    // session_key is set and then clears and persists the messages unconditionally, so the
    // promise was false on the commonest condition it described, and the loss is
    // irreversible. It belongs with the backend unit that answers busy/409, not here.
    const copy = i18nT('pages.channelPage.this_will_reset_conversation_history_for_all_age')
    expect(copy, 'the confirm reports the act as a no-op').not.toContain('kept instead')
    for (const promise of ['are kept', 'only the agents that are idle', 'still working']) {
      expect(copy, `the confirm promises "${promise}" while the handler clears everything`)
        .not.toContain(promise)
    }
    // What it must still say: the destruction it is asking consent for.
    expect(copy).toContain("deletes the channel's messages")

    const perAgent = i18nT('pages.channelPage.reset_role_s_llm_session_the_channel_s_shared_me')
    expect(perAgent, 'the per-agent tooltip carries the same false promise')
      .not.toContain('still working')
  })

  it('names the message deletion in the channel-wide confirm', async () => {
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await userEvent.click(screen.getByTitle('Clear all context'))

    const asked = String(
      (await screen.findByText(/deletes the channel/i)).textContent ?? '',
    )
    expect(asked).toMatch(/delete/i)
    expect(asked).toMatch(/message/i)
    expect(
      screen.getByRole('button', { name: 'Clear context and delete messages' }),
      'the confirming button must restate the destructive act',
    ).toBeInTheDocument()
  })

  it('acknowledges a clean clear instead of rendering nothing', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClearContext = vi.fn().mockResolvedValue({
      ok: true,
      cleared: ['Scribe'],
      busy: [],
    })

    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()

    const done = await screen.findByTestId('clear-context-done')
    expect(done).toHaveAttribute('role', 'status')
    expect(screen.queryByTestId('clear-context-error')).toBeNull()
  })

  it('scrolls the clean-clear acknowledgment into view', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const scrollSpy = vi.fn()
    // jsdom does not implement scrollIntoView, so the prototype is the observation point.
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: scrollSpy,
    })
    vi.mocked(api).channelClearContext = vi.fn().mockResolvedValue({
      ok: true,
      cleared: ['Scribe'],
      busy: [],
    })

    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()

    const done = await screen.findByTestId('clear-context-done')
    await waitFor(() => expect(scrollSpy).toHaveBeenCalled())
    expect(done).toBeTruthy()
  })

  it('does not call API when confirm is cancelled', async () => {
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())
    await userEvent.click(screen.getByTitle('Clear all context'))
    await waitFor(() => screen.getByRole('button', { name: 'Cancel' }))
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(vi.mocked(api).channelClearContext).not.toHaveBeenCalled()
  })

  it('re-fetches channel data after successful clear', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    vi.mocked(api).channelGet.mockClear()  // ignore the initial-render fetch
    await confirmClearAll()
    await waitFor(() => expect(vi.mocked(api).channelGet).toHaveBeenCalledWith('ch1'))
  })

  it('reports an API failure through the in-page ErrorNotice, not a native alert', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {})
    vi.mocked(api).channelClearContext = vi.fn().mockRejectedValue(new Error('server error'))
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()
    // Its OWN notice, not the page-wide one: this can render above an unsent composer
    // draft, so it must not carry the agent hand-off that would unmount the page.
    const notice = await screen.findByTestId('clear-context-error')
    expect(notice.textContent).toContain('Failed to clear context')
    expect(notice.textContent).toContain('server error')
    expect(notice.textContent).not.toContain('Ask the agent')
    // One register for every notice: `errors-use-error-notice` forbids a warn-toned variant
    // of this component, so a withheld clear wears the same chrome and says what survived in
    // its words instead. This arm pins that the softer register never comes back.
    expect(notice.className).toContain('border-danger')
    expect(notice.className).not.toContain('border-warn')
    expect(alertSpy).not.toHaveBeenCalled()
  })

  it("says the channel is gone on a 404, not the server's bare 'not found'", async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClearContext = vi.fn().mockRejectedValue(
      new ApiError(404, JSON.stringify({ error: 'not found' })),
    )
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()
    const notice = await screen.findByTestId('clear-context-error')
    expect(notice.textContent).toContain(i18nT('pages.channelPage.clear_context_channel_gone'))
    expect(notice.textContent).not.toContain('not found')
  })

  it("says the agent is gone on a per-agent 404, not the server's bare 'agent not found'", async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClearContext = vi.fn().mockRejectedValue(
      new ApiError(404, JSON.stringify({ error: 'agent not found' })),
    )
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByRole('button', { name: '1 agent' }))
    await userEvent.click(screen.getByRole('button', { name: '1 agent' }))
    await waitFor(() => screen.getByTitle('Clear context'))
    await userEvent.click(screen.getByTitle('Clear context'))
    await answerAgentClear()
    const notice = await screen.findByTestId('clear-context-error')
    expect(notice.textContent).toContain(i18nT('pages.channelPage.clear_context_agent_gone'))
    expect(notice.textContent).not.toContain('not found')
  })

  it('spins the header opener while its request runs, so a slow clear does not read as dead', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    let release: (v: unknown) => void = () => {}
    vi.mocked(api).channelClearContext = vi.fn().mockImplementation(
      () => new Promise(resolve => { release = resolve })
    )
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    const btn = screen.getByTitle('Clear all context')
    expect(btn.querySelector('.animate-spin')).toBeNull()
    await confirmClearAll()
    await waitFor(() => expect(btn).toHaveAttribute('aria-busy', 'true'))
    expect(btn.querySelector('.animate-spin'), 'the busy opener only greys out').not.toBeNull()
    await act(async () => { release({ ok: true, cleared: ['Researcher'] }) })
    await waitFor(() => expect(btn).toHaveAttribute('aria-busy', 'false'))
    expect(btn.querySelector('.animate-spin')).toBeNull()
  })

  it('surfaces a failed post-clear refresh instead of leaving the view silently stale', async () => {
    // The clear LANDED, so the clear-context banner must not claim failure -- but the redraw
    // did not, and swallowing it left the page showing pre-clear state with nothing saying so.
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClearContext = vi.fn().mockResolvedValue({ ok: true, cleared: ['Researcher'] })
    // Succeeds for the page's own load, fails ONLY for the post-clear redraw -- otherwise the
    // notice appears from the initial load and the test passes without the fix.
    const okChannel = await vi.mocked(api).channelGet('ch1')
    vi.mocked(api).channelGet = vi.fn()
      .mockResolvedValueOnce(okChannel)
      .mockRejectedValue(new Error('refresh boom'))
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()

    const notice = await screen.findByText(/refresh boom/)
    expect(notice).toBeTruthy()
    // And NOT through the clear-context notice, which would report the clear as failed.
    expect(screen.queryByTestId('clear-context-error')).toBeNull()
  })

  it('does not claim failure when the clear succeeded and only the refresh threw', async () => {
    // The refresh is a redraw, not the operation. Reporting its failure as the clear's sends
    // the user back through the confirm to re-clear work that is already gone.
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClearContext = vi.fn().mockResolvedValue({ ok: true, busy: [] })
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    vi.mocked(api).channelGet = vi.fn().mockRejectedValue(new Error('refresh exploded'))
    await confirmClearAll()
    await waitFor(() => expect(api.channelClearContext).toHaveBeenCalled())
    expect(screen.queryByTestId('clear-context-error')).toBeNull()
  })

  it("keeps channel B's notice when a clean clear for channel A resolves after the switch", async () => {
    // A's late success must not erase B's notice: nothing puts it back, so the user is left
    // believing B's clear reported nothing.
    const other = { ...mockChannel, id: 'ch2', topic: 'Second Channel' }
    vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [mockChannel, other] })
    vi.mocked(api).channelGet = vi.fn().mockImplementation(async (id: string) =>
      id === 'ch2' ? other : mockChannel,
    )
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    let releaseA: (v: unknown) => void = () => {}
    const aPending = new Promise(resolve => {
      releaseA = resolve
    })
    vi.mocked(api).channelClearContext = vi.fn().mockImplementation(async (id: string) => {
      if (id === 'ch1') {
        await aPending
        // A CLEAN result, which is the branch that resets the notice.
        return { ok: true, cleared: ['Researcher'] }
      }
      throw new Error('channel B failure')
    })

    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()

    await userEvent.click(screen.getByText('Second Channel'))
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()
    const notice = await screen.findByTestId('clear-context-error')
    expect(notice.textContent).toContain('channel B failure')

    // `onSuccess` writes the notice, THEN awaits `channelGet(A)`, so a call for A orders the write.
    vi.mocked(api).channelGet.mockClear()
    releaseA({})
    await waitFor(() => expect(api.channelGet).toHaveBeenCalledWith('ch1'))

    const still = screen.queryByTestId('clear-context-error')
    expect(still).not.toBeNull()
    expect(still?.textContent).toContain('channel B failure')
  })

  it('a late FAILURE on the channel the user left keeps the notice they are reading', async () => {
    // The OTHER write: A fails late and replaces the shared notice with one scoped to A,
    // which the display scope renders as nothing at all.
    const other = { ...mockChannel, id: 'ch2', topic: 'Second Channel' }
    vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [mockChannel, other] })
    vi.mocked(api).channelGet = vi.fn().mockImplementation(async (id: string) =>
      id === 'ch2' ? other : mockChannel,
    )
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    let failA: (e: unknown) => void = () => {}
    const aPending = new Promise((_resolve, reject) => {
      failA = reject
    })
    vi.mocked(api).channelClearContext = vi.fn().mockImplementation(async (id: string) => {
      if (id === 'ch1') {
        await aPending
        return { ok: true }
      }
      throw new Error('channel B failure')
    })

    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()

    await userEvent.click(screen.getByText('Second Channel'))
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()
    const notice = await screen.findByTestId('clear-context-error')
    expect(notice.textContent).toContain('channel B failure')

    failA(new Error('server error'))

    // A's off-screen failure takes the page notice, so its arrival orders the assertion below.
    await screen.findByTestId('channel-error')
    const still = screen.queryByTestId('clear-context-error')
    expect(still).not.toBeNull()
    expect(still?.textContent).toContain('channel B failure')
  })

  it('does not dress a clean deletion in the warning hue', async () => {
    // only by reading the words; the deletion carries its weight instead.
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClearContext = vi.fn().mockResolvedValue({
      ok: true, cleared: ['Researcher'], messages_deleted: true,
    })
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()
    const done = await screen.findByTestId('clear-context-done')
    expect(done.className, 'a clean clear wears the warning colour').not.toContain('text-warn')
    expect(done.className, 'the deletion must still stand out from a plain clear').toContain(
      'font-medium',
    )
    // The EMITTED utility: the colour token is named `text-strong`, so a bare `text-strong`
    // class names a colour called `strong`, which no theme declares -- it renders colourless.
    expect(done.className, 'a phantom colour class renders with no colour at all').toContain(
      'text-text-strong',
    )
  })

  it('clears a single agent via the agents panel with scope=agent', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByRole('button', { name: '1 agent' }))
    await userEvent.click(screen.getByRole('button', { name: '1 agent' }))  // open agents sidebar
    await waitFor(() => screen.getByTitle('Clear context'))
    await userEvent.click(screen.getByTitle('Clear context'))
    await answerAgentClear()
    await waitFor(() => expect(vi.mocked(api).channelClearContext).toHaveBeenCalledWith('ch1', 'agent', 'a1'))
  })

  it('names the role when a per-agent clear succeeds, not a channel-wide claim', async () => {
    // "Context cleared." after clearing ONE member reads as the whole channel.
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClearContext.mockResolvedValueOnce({
      cleared: ['Analyst'], busy: [],
    } as never)
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByRole('button', { name: '1 agent' }))
    await userEvent.click(screen.getByRole('button', { name: '1 agent' }))
    await waitFor(() => screen.getByTitle('Clear context'))
    await userEvent.click(screen.getByTitle('Clear context'))
    await answerAgentClear()

    const done = await screen.findByTestId('clear-context-done')
    expect(done.textContent).toContain('Analyst')
    expect(done.textContent).not.toBe('Context cleared.')
  })

  it('confirms a per-agent clear at the row that was clicked', async () => {
    // The button only re-enables and the page-top line is away from the panel, so without a
    // mark at the row a per-agent clear reads as having done nothing.
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByRole('button', { name: '1 agent' }))
    await userEvent.click(screen.getByRole('button', { name: '1 agent' }))  // open agents sidebar
    await waitFor(() => screen.getByTitle('Clear context'))
    await userEvent.click(screen.getByTitle('Clear context'))
    await answerAgentClear()

    const mark = await screen.findByTestId('agent-clear-done')
    expect(mark.getAttribute('aria-label')).toBeTruthy()
    // Replaces that row's clear button rather than sitting beside it: at this panel's fixed
    // width an extra control squeezed the role name down to one glyph.
    expect(
      screen.queryAllByTitle('Clear context'),
      'the mark added a control instead of taking the cleared button\'s slot',
    ).toHaveLength(0)
    expect(screen.getByText('Researcher')).toBeInTheDocument()
  })

  it('keeps the pressed row button focused after the clear, rather than dropping to the body', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByRole('button', { name: '1 agent' }))
    await userEvent.click(screen.getByRole('button', { name: '1 agent' }))
    await waitFor(() => screen.getByTitle('Clear context'))
    const pressed = screen.getByTitle('Clear context')
    await userEvent.click(pressed)
    await answerAgentClear()

    const mark = await screen.findByTestId('agent-clear-done')
    expect(mark, 'the cleared row mounted a different element, so the press lost its focus').toBe(pressed)
    // The confirm hands focus back to its opener once it has animated out.
    await waitFor(() => expect(
      document.activeElement,
      'focus fell to the body, leaving a keyboard user with no position in the panel',
    ).toBe(pressed))
    expect(mark.getAttribute('aria-disabled')).toBe('true')
  })

  it('shows the success mark at full strength, not in the disabled fade', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByRole('button', { name: '1 agent' }))
    await userEvent.click(screen.getByRole('button', { name: '1 agent' }))
    await waitFor(() => screen.getByTitle('Clear context'))
    await userEvent.click(screen.getByTitle('Clear context'))
    await answerAgentClear()

    const mark = await screen.findByTestId('agent-clear-done')
    expect(
      mark.getAttribute('aria-disabled'),
      'the done state stopped announcing itself as non-interactive',
    ).toBe('true')
    expect(
      mark.className,
      'the acknowledgment carries the in-flight fade, so the check renders greyed like a disabled control',
    ).not.toContain('aria-disabled:opacity-30')
    expect(
      mark.getAttribute('title'),
      'the mark repeats the header line that already says what was cleared',
    ).toBeNull()
  })

  it('keeps the deletion receipt until it is dismissed, rather than withdrawing it on a timer', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())
    await confirmClearAll()
    await waitFor(() => screen.getByTestId('clear-context-done'))

    await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
    expect(
      screen.queryByTestId('clear-context-done'),
      'the only proof the messages were deleted withdrew itself, so looking away loses it',
    ).not.toBeNull()

    await userEvent.click(screen.getByTestId('clear-context-done-dismiss'))
    await waitFor(() => expect(screen.queryByTestId('clear-context-done')).toBeNull())
  })

  it('still withdraws the per-agent acknowledgment on its timer', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByRole('button', { name: '1 agent' }))
    await userEvent.click(screen.getByRole('button', { name: '1 agent' }))
    await waitFor(() => screen.getByTitle('Clear context'))
    await userEvent.click(screen.getByTitle('Clear context'))
    await answerAgentClear()
    await waitFor(() => screen.getByTestId('agent-clear-done'))

    await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
    expect(
      screen.queryByTestId('agent-clear-done'),
      'the row mark outlived its timer, so a row stays marked for the rest of the session',
    ).toBeNull()
  })
})
