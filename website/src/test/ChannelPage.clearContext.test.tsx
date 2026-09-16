import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ChannelPage from '../pages/ChannelPage'
import { renderWithProviders } from './helpers'
import { api, ApiError } from '../api/client'
import { clearContextBusyMessage, clearContextBusyRefusal } from '../pages/ChannelPage'
import { initI18n } from '../i18n/all'
import { i18nT } from '../i18n/t'

// PARTIAL, not an automock: the helper under test narrows on `e instanceof ApiError`, and an
// automocked class makes that fail for both the test and the component that imports it.
vi.mock('../api/client', async importOriginal => {
  const actual = await importOriginal<typeof import('../api/client')>()
  const stub = Object.fromEntries(Object.keys(actual.api).map(k => [k, vi.fn()]))
  return { ...actual, api: stub as unknown as typeof actual.api }
})

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
      screen.getByText(/failed to load team presets/i),
      'the presets failure was erased by the first channel selection',
    ).toBeInTheDocument()
  })

  it('drops a refusal that lands after the user has moved to another channel', async () => {
    // GPT 5.6's finding. The result is stored keyed by the channel it was issued for, so a
    // late refusal for ch1 used to sit in state and RESURFACE the moment the user came back
    // to ch1 -- advertising "still working" about a turn that finished long before. Results
    // for a channel that is not the one on screen are now discarded outright.
    vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [mockChannel, mockChannel2] })
    // id-AWARE, unlike the shared beforeEach stub: returning ch1 for every read would drop ch2
    // out of `channels`, the render would fall back to channels[0]=ch1, and the assertion
    // below would pass for the wrong reason -- ch1 really being on screen.
    vi.mocked(api).channelGet = vi.fn().mockImplementation(
      (id: string) => Promise.resolve(id === 'ch2' ? mockChannel2 : mockChannel),
    )
    let release: (v: unknown) => void = () => {}
    vi.mocked(api).channelClearContext = vi.fn().mockImplementation(
      () => new Promise(resolve => { release = resolve }),
    )
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())
    await confirmClearAll()

    // Move to ch2 while the clear for ch1 is still in flight.
    await userEvent.click(screen.getByText('Second Channel'))
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())

    // ch1's refusal arrives now, addressed to a channel that is no longer rendered.
    await act(async () => {
      release({ ok: false, busy: ['Researcher'], cleared: [] })
      await Promise.resolve()
    })
    expect(
      screen.queryByTestId('clear-context-error'),
      'an off-screen refusal was rendered over the channel the user is now reading',
    ).toBeNull()

    // Returning to ch1 must not resurrect it either.
    await userEvent.click(screen.getByText('Test Channel'))
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())
    expect(
      screen.queryByTestId('clear-context-error'),
      'the stale refusal came back on return to its channel',
    ).toBeNull()
  })

  it('does not let a late success on another channel erase this one\'s acknowledgment', async () => {
    // Opus 5's finding: `setClearDone` was the one writer in `noteClearRefusal` without the
    // off-screen guard the two `setClearError` writers carry. With a clear in flight on ch1, a
    // switch to ch2 and a clear there, ch1's late success overwrote `clearDone` with a record
    // addressed to ch1 -- and ch2's just-rendered "Context cleared." line vanished.
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

  it('marks the header button busy while the clear-all request is in flight', async () => {
    // The refusal renders above the composer, away from this button, so a slow clear leaves
    // the click unacknowledged unless the button itself reacts.
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

    await userEvent.click(btn)
    expect(
      screen.queryByRole('button', { name: 'Clear context and delete messages' }),
      'clicking the busy button re-opened the confirm dialog',
    ).toBeNull()

    release({ ok: true, cleared: ['Researcher'] })
    await waitFor(() => expect(btn).not.toHaveAttribute('aria-disabled', 'true'))
  })

  it('joins refusing roles with the locale list format, not a Latin comma', () => {
    // A Latin ", " inside a zh-CN / ja / bn sentence reads as untranslated residue, so the
    // list goes through Intl.ListFormat. In `en` that is "A and B" rather than "A, B".
    const msg = clearContextBusyMessage({ cleared: [], busy: ['Scribe', 'Analyst'] })
    expect(msg).not.toContain('Scribe, Analyst')
    expect(msg).toContain('Scribe and Analyst')
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

  it('drops a stale clear-context refusal naming channel A roles when switching to channel B', async () => {
    // Nothing else clears it: the only other path is the user dismissing it by hand, so
    // it would read as a live refusal for whichever channel the composer now sends to.
    const other = { ...mockChannel, id: 'ch2', topic: 'Second Channel' }
    vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [mockChannel, other] })
    vi.mocked(api).channelGet = vi.fn().mockImplementation(async (id: string) =>
      id === 'ch2' ? other : mockChannel,
    )
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClearContext = vi.fn().mockRejectedValue(new Error('server error'))
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()
    const notice = await screen.findByTestId('clear-context-error')
    expect(notice.textContent).toContain('server error')

    await userEvent.click(screen.getByText('Second Channel'))

    await waitFor(() =>
      expect(screen.queryByTestId('clear-context-error')).toBeNull(),
      { timeout: 2000 },
    )
  })

  it('keeps channel B refusal when a clean clear for channel A resolves after the switch', async () => {
    // A's late success must not erase B's notice: nothing puts it back, so the user is left
    // sending into a channel whose members are still busy with nothing saying so.
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
        // A CLEAN result: nothing refused, which is the branch that resets the notice.
        return { ok: true, cleared: ['Researcher'] }
      }
      throw new ApiError(
        409,
        'conflict',
        JSON.stringify({ code: 'turn_in_flight', busy: ['Scribe'] }),
      )
    })

    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()

    await userEvent.click(screen.getByText('Second Channel'))
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()
    const notice = await screen.findByTestId('clear-context-error')
    expect(notice.textContent).toContain('Scribe')

    releaseA({})

    // B's refusal must still be on screen after A's clean result lands.
    await new Promise(resolve => setTimeout(resolve, 50))
    const still = screen.queryByTestId('clear-context-error')
    expect(still).not.toBeNull()
    expect(still?.textContent).toContain('Scribe')
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
      throw new ApiError(
        409,
        'conflict',
        JSON.stringify({ code: 'turn_in_flight', busy: ['Scribe'] }),
      )
    })

    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()

    await userEvent.click(screen.getByText('Second Channel'))
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()
    const notice = await screen.findByTestId('clear-context-error')
    expect(notice.textContent).toContain('Scribe')

    failA(new ApiError(500, 'server error', ''))

    await new Promise(resolve => setTimeout(resolve, 50))
    const still = screen.queryByTestId('clear-context-error')
    expect(still).not.toBeNull()
    expect(still?.textContent).toContain('Scribe')
  })

  it('never shows channel A refusal that resolves only after the switch to channel B', async () => {
    // The switch-time effect cannot reach a request still in flight, so A's refusal lands
    // afterwards and reads as live for B, whose roles it does not even name.
    const other = { ...mockChannel, id: 'ch2', topic: 'Second Channel' }
    vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [mockChannel, other] })
    vi.mocked(api).channelGet = vi.fn().mockImplementation(async (id: string) =>
      id === 'ch2' ? other : mockChannel,
    )
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    let releaseA: (v: unknown) => void = () => {}
    const pending = new Promise(res => { releaseA = res })
    vi.mocked(api).channelClearContext = vi.fn().mockImplementation(async () => {
      await pending
      return { ok: true, busy: ['Researcher'], cleared: [] }
    })
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()

    await userEvent.click(screen.getByText('Second Channel'))
    releaseA({})

    await waitFor(() => expect(api.channelClearContext).toHaveBeenCalled())
    expect(screen.queryByTestId('clear-context-error')).toBeNull()
  })

  it('leads a partial clear with a partial title, not the bold failure lead', async () => {
    // A bold "Failed to clear context" over a body that ends "Cleared for Analyst." reads
    // as a total failure, sending the user back to re-clear what already cleared.
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClearContext = vi.fn().mockResolvedValue({
      ok: true, busy: ['Researcher'], cleared: ['Analyst'],
    })
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()
    const notice = await screen.findByTestId('clear-context-error')
    expect(notice.textContent).toContain('Cleared for Analyst.')
    expect(notice.textContent).toContain('Context partially cleared')
    expect(notice.textContent).not.toContain('Failed to clear context')
    // Rewritten, not deleted: this used to require warn chrome on a partial success. The
    // remedy for "it reads as a failure" is the WORDS -- the title says partially cleared and
    // the body names what survived -- because an error-sourced value must not be toned down.
    expect(notice.className).toContain('border-danger')
    expect(notice.className).not.toContain('border-warn')
  })

  it('announces a withheld clear as assertively as a real failure', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    // INVERTED deliberately. This arm used to require role=status and warn chrome for a
    // withheld clear. `errors-use-error-notice` is blocking and says the opposite: an
    // error-sourced value -- and a 200-with-`busy` or a 409 both are -- must not render in a
    // polite status region or a warn tone, because toning a failure down does not make it
    // one. A user whose clear did NOT happen needs to be interrupted; what makes it honest
    // rather than alarming is the title and body naming what survived, asserted above.
    vi.mocked(api).channelClearContext = vi.fn().mockResolvedValue({
      ok: false, busy: ['Researcher'], cleared: [],
    })
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()
    const withheld = await screen.findByTestId('clear-context-error')
    expect(withheld.className).toContain('border-danger')
    expect(withheld.className, 'the banned warn register came back').not.toContain('border-warn')
    expect(withheld.getAttribute('role'), 'a withheld clear must not be toned to a status').toBe(
      'alert',
    )
    expect(
      withheld.textContent,
      'the reassurance has to be in the words, since it is no longer in the chrome',
    ).toContain('Context not cleared')

    await userEvent.click(screen.getByLabelText('Dismiss'))
    vi.mocked(api).channelClearContext = vi
      .fn()
      .mockRejectedValue(new Error('channel store unavailable'))
    await confirmClearAll()
    const failed = await screen.findByTestId('clear-context-error')
    expect(failed.className).toContain('border-danger')
    expect(failed.getAttribute('role'), 'a real failure must still announce assertively').toBe(
      'alert',
    )
  })

  it('marks the row whose clear was refused, not only the row that succeeded', async () => {
    // The banner lands above the composer, outside the agents panel the user is watching, so
    // a button that merely re-enables is indistinguishable from one that did nothing.
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClearContext = vi.fn().mockResolvedValue({
      ok: false, busy: ['Researcher'], cleared: [],
    })
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByRole('button', { name: '1 agent' }))
    await userEvent.click(screen.getByRole('button', { name: '1 agent' }))  // open agents sidebar
    await waitFor(() => screen.getByTitle('Clear context'))
    await userEvent.click(screen.getByTitle('Clear context'))
    await screen.findByTestId('clear-context-error')
    const kept = await screen.findByTestId('agent-clear-kept')
    expect(kept).toBeInTheDocument()
    expect(screen.queryByTestId('agent-clear-done')).toBeNull()
  })

  it('does not dress a clean deletion in the refusal hue', async () => {
    // Warn amber in the same slot as the refusal banner makes success and refusal legible
    // only by reading the words; the deletion carries its weight instead.
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClearContext = vi.fn().mockResolvedValue({
      ok: true, cleared: ['Researcher'], messages_deleted: true,
    })
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()
    const done = await screen.findByTestId('clear-context-done')
    expect(done.className, 'a clean clear wears the refusal colour').not.toContain('text-warn')
    expect(done.className, 'the deletion must still stand out from a plain clear').toContain(
      'font-medium',
    )
    // The EMITTED utility: the colour token is named `text-strong`, so a bare `text-strong`
    // class names a colour called `strong`, which no theme declares -- it renders colourless.
    expect(done.className, 'a phantom colour class renders with no colour at all').toContain(
      'text-text-strong',
    )
  })

  it('keeps the failure lead when a busy refusal cleared nothing at all', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClearContext = vi.fn().mockResolvedValue({
      ok: true, busy: ['Researcher'], cleared: [],
    })
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()
    const notice = await screen.findByTestId('clear-context-error')
    expect(notice.textContent).toContain('Context not cleared')
    expect(notice.textContent).not.toContain('Failed to clear context')
    expect(notice.textContent).not.toContain('Context partially cleared')
    // Withheld, not broken -- and that is carried by the LEAD, not by the chrome: the notice
    // keeps the one alert register `errors-use-error-notice` allows, while the title says
    // "Context not cleared" rather than claiming a failure.
    expect(notice.className).toContain('border-danger')
    expect(notice.className).not.toContain('border-warn')
  })

  it('drops the refusal banner once a retry finally clears cleanly', async () => {
    // The banner tells the user to retry when the busy roles finish; if the successful
    // retry leaves it mounted, the advice it gives is about an attempt already superseded.
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClearContext = vi.fn()
      .mockResolvedValueOnce({ ok: true, busy: ['Researcher'], cleared: [] })
      .mockResolvedValueOnce({ ok: true, busy: [], cleared: ['Researcher'] })
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await confirmClearAll()
    await screen.findByTestId('clear-context-error')

    await confirmClearAll()

    await waitFor(() =>
      expect(screen.queryByTestId('clear-context-error')).toBeNull(),
      { timeout: 2000 },
    )
  })

  it('clears a single agent via the agents panel with scope=agent', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByRole('button', { name: '1 agent' }))
    await userEvent.click(screen.getByRole('button', { name: '1 agent' }))  // open agents sidebar
    await waitFor(() => screen.getByTitle('Clear context'))
    await userEvent.click(screen.getByTitle('Clear context'))
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

    const mark = await screen.findByTestId('agent-clear-done')
    expect(mark.getAttribute('aria-label')).toBeTruthy()
  })

  it('renders a refused per-agent clear through the shared notice, not a second time in the row', async () => {
    // The row repeated the refusal text beside the button, putting error-derived copy in
    // bespoke markup while the notice above the composer already carried it.
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClearContext = vi.fn().mockResolvedValue({ cleared: [], busy: ['Researcher'] })

    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByRole('button', { name: '1 agent' }))
    await userEvent.click(screen.getByRole('button', { name: '1 agent' }))
    await waitFor(() => screen.getByTitle('Clear context'))
    await userEvent.click(screen.getByTitle('Clear context'))

    // The notice is the renderer, and it must actually carry the refusal -- otherwise removing
    // the row copy would have deleted the only place the user could read it.
    const notice = await screen.findByTestId('clear-context-error')
    expect(notice.textContent).toContain('Context not cleared')
    expect(screen.queryByTestId('agent-clear-refused')).toBeNull()
  })
})

/**
 * The clear-context click's decision about what the user is owed.
 *
 * A PARTIAL refusal answers 200 with the refusing roles in `busy`, so the
 * caller's catch never sees it and only reading that field keeps the click
 * honest. Before this helper existed the field had no reader at all, so a
 * refused clear rendered as a successful one.
 */
describe('clearContextBusyMessage', () => {
  beforeAll(() => {
    initI18n('en')
  })

  it('names every refusing role, so the user knows what to retry', () => {
    const msg = clearContextBusyMessage({ busy: ['Researcher', 'Analyst'] })
    expect(msg).toContain('Researcher')
    expect(msg).toContain('Analyst')
  })

  it('is empty when nothing refused, so a clean clear raises no dialog', () => {
    expect(clearContextBusyMessage({ busy: [] })).toBe('')
  })

  it('is empty for a response that omits the field entirely', () => {
    expect(clearContextBusyMessage({})).toBe('')
    expect(clearContextBusyMessage(null)).toBe('')
    expect(clearContextBusyMessage(undefined)).toBe('')
  })

  it('names the roles that DID clear, so a partial refusal does not read as a total one', () => {
    const msg = clearContextBusyMessage({ busy: ['Researcher'], cleared: ['Scribe', 'Analyst'] })
    expect(msg).toContain('Researcher')
    expect(msg).toContain('Scribe and Analyst')
  })

  it('omits the cleared clause when nothing cleared, so a total refusal claims nothing', () => {
    const msg = clearContextBusyMessage({ busy: ['Researcher'], cleared: [] })
    expect(msg).toContain('Researcher')
    expect(msg).not.toContain('Cleared for')
  })

  it('ignores a non-array busy value rather than rendering "[object Object]"', () => {
    expect(clearContextBusyMessage({ busy: 'Researcher' })).toBe('')
    expect(clearContextBusyMessage({ busy: { role: 'Researcher' } })).toBe('')
  })
})

/**
 * The same refusal, arriving as a THROW.
 *
 * A total refusal answers 409 rather than 200, so it never reaches the helper
 * above. The page's generic `fail` would render the backend's prose through
 * `apiError` -- doubled phrasing, and untranslated on a localized page -- so the
 * 409 is recognised by its code and rendered from the catalog like the partial
 * case. Everything else answers '' and is left to `fail`.
 */
describe('clearContextBusyRefusal', () => {
  beforeAll(() => {
    initI18n('en')
  })

  it('renders the localized refusal for a 409, not the backend prose', () => {
    const body = JSON.stringify({
      error: 'context not cleared: Researcher had a turn in flight. Nothing was cleared — retry when idle.',
      code: 'turn_in_flight',
      busy: ['Researcher'],
    })
    const msg = clearContextBusyRefusal(new ApiError(409, 'conflict', body))
    expect(msg).toBe(clearContextBusyMessage({ busy: ['Researcher'] }))
    expect(msg).not.toContain('Nothing was cleared')
  })

  it('names every refusing role on a total refusal', () => {
    const body = JSON.stringify({ code: 'turn_in_flight', busy: ['Researcher', 'Analyst'] })
    const msg = clearContextBusyRefusal(new ApiError(409, 'conflict', body))
    expect(msg).toContain('Researcher')
    expect(msg).toContain('Analyst')
  })

  it('defers a 409 that is a different conflict to the generic path', () => {
    const body = JSON.stringify({ error: 'nope', code: 'some_other_conflict' })
    expect(clearContextBusyRefusal(new ApiError(409, 'boom', body))).toBe('')
  })

  it('defers a 409 whose body is not JSON at all', () => {
    expect(clearContextBusyRefusal(new ApiError(409, 'boom', '<html>502</html>'))).toBe('')
  })

  it('defers a 409 that carries the code but no roles', () => {
    const body = JSON.stringify({ code: 'turn_in_flight', busy: [] })
    expect(clearContextBusyRefusal(new ApiError(409, 'boom', body))).toBe('')
  })

  it('leaves a non-409 failure to the generic path', () => {
    expect(clearContextBusyRefusal(new ApiError(500, 'server error', ''))).toBe('')
    expect(clearContextBusyRefusal(new Error('network down'))).toBe('')
  })

  it('leaves a thrown non-Error to the generic path', () => {
    expect(clearContextBusyRefusal('a bare string')).toBe('')
  })
})
