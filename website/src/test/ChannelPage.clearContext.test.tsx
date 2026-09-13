import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ChannelPage from '../pages/ChannelPage'
import { renderWithProviders } from './helpers'
import { api, ApiError } from '../api/client'
import { clearContextBusyMessage, clearContextBusyRefusal } from '../pages/ChannelPage'
import { initI18n } from '../i18n/all'

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

  it('calls channelClearContext with scope=all on confirm', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())
    await userEvent.click(screen.getByTitle('Clear all context'))
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

    await waitFor(() => expect(btn).toBeDisabled())
    expect(btn).toHaveAttribute('aria-busy', 'true')

    release({ ok: true, cleared: ['Researcher'] })
    await waitFor(() => expect(btn).not.toBeDisabled())
  })

  it('offers a retry inside the refusal notice that re-issues the recorded scope', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClearContext = vi.fn()
      .mockRejectedValueOnce(
        new ApiError(409, 'conflict', JSON.stringify({ code: 'turn_in_flight', busy: ['Scribe'] })),
      )
      .mockResolvedValueOnce({ ok: true, cleared: ['Scribe'] })

    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await userEvent.click(screen.getByTitle('Clear all context'))

    const notice = await screen.findByTestId('clear-context-error')
    const retry = await screen.findByTestId('clear-context-retry')
    expect(notice.contains(retry)).toBe(true)

    await userEvent.click(retry)
    await waitFor(() => expect(vi.mocked(api).channelClearContext).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api).channelClearContext).toHaveBeenLastCalledWith('ch1', 'all')
  })

  it('joins refusing roles with the locale list format, not a Latin comma', () => {
    // A Latin ", " inside a zh-CN / ja / bn sentence reads as untranslated residue, so the
    // list goes through Intl.ListFormat. In `en` that is "A and B" rather than "A, B".
    const msg = clearContextBusyMessage({ cleared: [], busy: ['Scribe', 'Analyst'] })
    expect(msg).not.toContain('Scribe, Analyst')
    expect(msg).toContain('Scribe and Analyst')
  })

  it('asks again before the channel-wide retry deletes the messages', async () => {
    // A banner sentence is not consent: the retry re-runs the same destructive act, so it must
    // ask again rather than delete the channel's messages on one click.
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClearContext = vi.fn().mockRejectedValueOnce(
      new ApiError(409, 'conflict', JSON.stringify({ code: 'turn_in_flight', busy: ['Scribe'] })),
    )

    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await userEvent.click(screen.getByTitle('Clear all context'))

    const notice = await screen.findByTestId('clear-context-error')
    // The banner said the messages were kept and then that trying again deletes them; the
    // confirm below is the one place that consequence belongs.
    expect(notice.textContent).not.toMatch(/also deletes the channel's messages/i)

    const retry = await screen.findByTestId('clear-context-retry')
    await userEvent.click(retry)

    expect(confirmSpy).toHaveBeenCalledTimes(2)
    expect(vi.mocked(api).channelClearContext).toHaveBeenCalledTimes(2)
  })

  it('abandons the channel-wide retry when its confirm is declined', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm')
    confirmSpy.mockReturnValueOnce(true).mockReturnValueOnce(false)
    vi.mocked(api).channelClearContext = vi.fn().mockRejectedValueOnce(
      new ApiError(409, 'conflict', JSON.stringify({ code: 'turn_in_flight', busy: ['Scribe'] })),
    )

    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await userEvent.click(screen.getByTitle('Clear all context'))

    const retry = await screen.findByTestId('clear-context-retry')
    await userEvent.click(retry)

    expect(confirmSpy).toHaveBeenCalledTimes(2)
    expect(vi.mocked(api).channelClearContext).toHaveBeenCalledTimes(1)
  })

  it('names the message deletion in the channel-wide confirm', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)

    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await userEvent.click(screen.getByTitle('Clear all context'))

    const asked = String(confirmSpy.mock.calls[0]?.[0] ?? '')
    expect(asked).toMatch(/delete/i)
    expect(asked).toMatch(/message/i)
  })

  it('offers Retry on a hard failure too, not only on a withheld refusal', async () => {
    // The refusal banner offers a next step; the same banner shape for a genuine failure
    // offered none, so the user was stranded in the surface that had just taught them to retry.
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClearContext = vi.fn().mockRejectedValueOnce(
      new ApiError(500, 'boom', JSON.stringify({ error: 'channel store unavailable' })),
    )

    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await userEvent.click(screen.getByTitle('Clear all context'))

    const notice = await screen.findByTestId('clear-context-error')
    expect(notice.textContent).toMatch(/Failed to clear context/i)
    const retry = await screen.findByTestId('clear-context-retry')
    expect(notice.contains(retry)).toBe(true)
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
    await userEvent.click(screen.getByTitle('Clear all context'))

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
    await userEvent.click(screen.getByTitle('Clear all context'))

    const done = await screen.findByTestId('clear-context-done')
    await waitFor(() => expect(scrollSpy).toHaveBeenCalled())
    expect(done).toBeTruthy()
  })

  it('does not call API when confirm is cancelled', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())
    await userEvent.click(screen.getByTitle('Clear all context'))
    expect(vi.mocked(api).channelClearContext).not.toHaveBeenCalled()
  })

  it('re-fetches channel data after successful clear', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    vi.mocked(api).channelGet.mockClear()  // ignore the initial-render fetch
    await userEvent.click(screen.getByTitle('Clear all context'))
    await waitFor(() => expect(vi.mocked(api).channelGet).toHaveBeenCalledWith('ch1'))
  })

  it('reports an API failure through the in-page ErrorNotice, not a native alert', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {})
    vi.mocked(api).channelClearContext = vi.fn().mockRejectedValue(new Error('server error'))
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await userEvent.click(screen.getByTitle('Clear all context'))
    // Its OWN notice, not the page-wide one: this can render above an unsent composer
    // draft, so it must not carry the agent hand-off that would unmount the page.
    const notice = await screen.findByTestId('clear-context-error')
    expect(notice.textContent).toContain('Failed to clear context')
    expect(notice.textContent).toContain('server error')
    expect(notice.textContent).not.toContain('Ask the agent')
    // A real error, not a withheld clear: this is the one case that stays red, which is
    // what makes the warn chrome on the two refusal shapes mean anything.
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
    await userEvent.click(screen.getByTitle('Clear all context'))

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
    await userEvent.click(screen.getByTitle('Clear all context'))
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
    await userEvent.click(screen.getByTitle('Clear all context'))
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
    await userEvent.click(screen.getByTitle('Clear all context'))

    await userEvent.click(screen.getByText('Second Channel'))
    await waitFor(() => screen.getByTitle('Clear all context'))
    await userEvent.click(screen.getByTitle('Clear all context'))
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
    await userEvent.click(screen.getByTitle('Clear all context'))

    await userEvent.click(screen.getByText('Second Channel'))
    await waitFor(() => screen.getByTitle('Clear all context'))
    await userEvent.click(screen.getByTitle('Clear all context'))
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
    await userEvent.click(screen.getByTitle('Clear all context'))

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
    await userEvent.click(screen.getByTitle('Clear all context'))
    const notice = await screen.findByTestId('clear-context-error')
    expect(notice.textContent).toContain('Cleared for Analyst.')
    expect(notice.textContent).toContain('Context partially cleared')
    expect(notice.textContent).not.toContain('Failed to clear context')
    // Colour is read before the words are: danger chrome on a partial SUCCESS reports a
    // failure that did not happen, and invites re-clearing the roles already done.
    expect(notice.className).toContain('border-warn')
    expect(notice.className).not.toContain('border-danger')
  })

  it('keeps the failure lead when a busy refusal cleared nothing at all', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.mocked(api).channelClearContext = vi.fn().mockResolvedValue({
      ok: true, busy: ['Researcher'], cleared: [],
    })
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await userEvent.click(screen.getByTitle('Clear all context'))
    const notice = await screen.findByTestId('clear-context-error')
    expect(notice.textContent).toContain('Context not cleared')
    expect(notice.textContent).not.toContain('Failed to clear context')
    expect(notice.textContent).not.toContain('Context partially cleared')
    // Withheld, not broken: the lead no longer claims a failure and the chrome agrees.
    expect(notice.className).toContain('border-warn')
    expect(notice.className).not.toContain('border-danger')
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
    await userEvent.click(screen.getByTitle('Clear all context'))
    await screen.findByTestId('clear-context-error')

    await userEvent.click(screen.getByTitle('Clear all context'))

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

  it('does not let Retry submit a duplicate while a per-agent clear is in flight', async () => {
    // `retryBusy` is keyed on the mutation, so a per-agent clear that bypasses it leaves
    // Retry live and one click sends a second clear for the same agent.
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    // First clear: a partial refusal, which is what raises the banner carrying Retry.
    vi.mocked(api).channelClearContext.mockResolvedValueOnce({
      cleared: [], busy: ['Analyst'],
    } as never)
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByRole('button', { name: '1 agent' }))
    await userEvent.click(screen.getByRole('button', { name: '1 agent' }))
    await waitFor(() => screen.getByTitle('Clear context'))
    await userEvent.click(screen.getByTitle('Clear context'))

    const retry = await screen.findByTestId('clear-context-retry')
    expect(retry).not.toBeDisabled()

    // Second per-agent clear, left in flight.
    let release: (v: unknown) => void = () => undefined
    vi.mocked(api).channelClearContext.mockImplementationOnce(
      () => new Promise(resolve => { release = resolve }),
    )
    await userEvent.click(screen.getByTitle('Clear context'))
    await waitFor(() => expect(vi.mocked(api).channelClearContext).toHaveBeenCalledTimes(2))

    await waitFor(() => expect(screen.getByTestId('clear-context-retry')).toBeDisabled())
    await userEvent.click(screen.getByTestId('clear-context-retry'))
    expect(vi.mocked(api).channelClearContext).toHaveBeenCalledTimes(2)

    release({ cleared: ['Analyst'], busy: [] })
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
