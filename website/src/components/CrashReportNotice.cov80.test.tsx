import { act, screen, waitFor, fireEvent, within } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import CrashReportNotice from './CrashReportNotice'
import { fmtDateTime } from '../i18n/format'

type Summary = { newCount: number; lastCrashAt?: string | null }

const get = vi.fn<[], Promise<Summary>>()
const reveal = vi.fn<[], Promise<{ ok: boolean; error?: string }>>()

/** Installs (or removes) the preload bridge the component reads off `window`. */
function bridge(present: boolean) {
  const w = window as unknown as { crashReportsAPI?: unknown }
  if (present) w.crashReportsAPI = { get, reveal }
  else delete w.crashReportsAPI
}

const LAST_CRASH_AT = '2026-10-28T19:19:55.283Z'

const refused = () => new Error('crash-reports:get is restricted to the local dashboard')

/** The whole re-ask schedule (15 s + 45 s + 2 + 5 + 10 min) plus a margin. */
const PAST_SCHEDULE_MS = 20 * 60_000

/** Lets every settled ask and every timer due within `ms` run. */
async function advance(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

function summary(over: Partial<Summary> = {}): Summary {
  return { newCount: 2, lastCrashAt: LAST_CRASH_AT, ...over }
}

const COUNT_TEXT = 'New crash records saved on this machine: 2. Attach them when you report the problem.'

describe('CrashReportNotice', () => {
  beforeEach(() => {
    get.mockReset()
    reveal.mockReset()
    get.mockResolvedValue(summary())
    reveal.mockResolvedValue({ ok: true })
    bridge(true)
  })
  afterEach(() => bridge(false))

  it('announces a crash count as a status region', async () => {
    renderWithProviders(<CrashReportNotice />)
    const notice = await screen.findByRole('status')
    expect(notice).toHaveTextContent(/closed unexpectedly/i)
    expect(notice).toHaveTextContent(/2/)
    // The stock transparent Btn has no edge on the banner's warn tint until hovered.
    for (const name of [/Show diagnostics/, /Report issue/]) {
      const button = within(notice).getByRole('button', { name })
      expect(button).toHaveClass('bg-bg-elevated', 'border-border-strong')
      expect(button).not.toHaveClass('bg-transparent')
      expect(button).not.toHaveClass('border-border')
    }
  })

  // The expected date comes from fmtDateTime: this test checks the line, and
  // format.test.ts owns the date format itself.
  it('shows when the newest crash happened, directly under the unchanged count', async () => {
    renderWithProviders(<CrashReportNotice />)
    const latest = await screen.findByTestId('crash-notice-latest')
    expect(latest).toHaveTextContent(`Latest crash: ${fmtDateTime(LAST_CRASH_AT)}`)
    const count = screen.getByText(COUNT_TEXT)
    expect(count.nextElementSibling).toBe(latest)
  })

  it('omits the time line when the main process sends lastCrashAt: null', async () => {
    get.mockResolvedValue(summary({ lastCrashAt: null }))
    renderWithProviders(<CrashReportNotice />)
    const notice = await screen.findByRole('status')
    expect(screen.getByText(COUNT_TEXT)).toBeInTheDocument()
    expect(screen.queryByTestId('crash-notice-latest')).not.toBeInTheDocument()
    expect(notice).not.toHaveTextContent(/latest crash/i)
  })

  // An older shell sends the count alone; a missing or malformed time must
  // never render as the formatter's "—" placeholder.
  it.each([
    ['an older shell that sends no lastCrashAt', { newCount: 2 }],
    ['a malformed lastCrashAt', { newCount: 2, lastCrashAt: 'not a time' }],
  ])('omits the time line for %s', async (_label, payload) => {
    get.mockResolvedValue(payload as Summary)
    renderWithProviders(<CrashReportNotice />)
    const notice = await screen.findByRole('status')
    expect(screen.getByText(COUNT_TEXT)).toBeInTheDocument()
    expect(screen.queryByTestId('crash-notice-latest')).not.toBeInTheDocument()
    expect(notice).not.toHaveTextContent('—')
  })

  it('never renders without the desktop bridge, and never probes for one', async () => {
    bridge(false)
    renderWithProviders(<CrashReportNotice />)
    await waitFor(() => expect(get).not.toHaveBeenCalled())
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('stays silent when nothing new crashed', async () => {
    get.mockResolvedValue(summary({ newCount: 0 }))
    renderWithProviders(<CrashReportNotice />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  // A rejection is what a connection window pointed at a remote gateway gets
  // from the sender gate. That is the correct answer, so it must not surface as
  // a broken banner or an unhandled rejection.
  it('stays silent when the sender gate refuses the request', async () => {
    get.mockRejectedValue(new Error('crash-reports:get is restricted to the local dashboard'))
    renderWithProviders(<CrashReportNotice />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('asks the main process to reveal the ledger, naming no path itself', async () => {
    renderWithProviders(<CrashReportNotice />)
    await screen.findByRole('status')
    fireEvent.click(screen.getByRole('button', { name: /show diagnostics/i }))
    expect(reveal).toHaveBeenCalledWith()
  })

  // The banner's one action must fail LOUD, not silent: a swallowed failure
  // leaves the user told to attach files they now cannot find.
  it('surfaces a rejected reveal through an error notice with the fallback path', async () => {
    reveal.mockRejectedValue(new Error('no crash log'))
    renderWithProviders(<CrashReportNotice />)
    await screen.findByRole('status')
    fireEvent.click(screen.getByRole('button', { name: /show diagnostics/i }))
    const err = await screen.findByTestId('crash-notice-reveal-error')
    expect(err).toHaveTextContent(/crashes\.log/i)
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  // `{ ok: false }` is the other failure shape (log removed since the scan) and
  // was discarded exactly like a rejection.
  it('surfaces an ok:false reveal result, not only a rejection', async () => {
    reveal.mockResolvedValue({ ok: false, error: 'no crash log' })
    renderWithProviders(<CrashReportNotice />)
    await screen.findByRole('status')
    fireEvent.click(screen.getByRole('button', { name: /show diagnostics/i }))
    const err = await screen.findByTestId('crash-notice-reveal-error')
    expect(err).toHaveTextContent(/crashes\.log/i)
  })

  it('lets the reveal error be dismissed on its own', async () => {
    reveal.mockResolvedValue({ ok: false })
    renderWithProviders(<CrashReportNotice />)
    await screen.findByRole('status')
    fireEvent.click(screen.getByRole('button', { name: /show diagnostics/i }))
    const err = await screen.findByTestId('crash-notice-reveal-error')
    fireEvent.click(within(err).getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByTestId('crash-notice-reveal-error')).not.toBeInTheDocument()
    // the crash banner itself is still there — only the error line cleared
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  it('shows no error notice when the reveal succeeds', async () => {
    renderWithProviders(<CrashReportNotice />)
    await screen.findByRole('status')
    fireEvent.click(screen.getByRole('button', { name: /show diagnostics/i }))
    await waitFor(() => expect(reveal).toHaveBeenCalled())
    expect(screen.queryByTestId('crash-notice-reveal-error')).not.toBeInTheDocument()
  })

  // The crash notice is where the user already knows something broke, so the
  // report starts here: the shared Report a Problem dialog, with the crash
  // context the banner holds (the count) already in the note.
  it('opens the report dialog with the crash pre-filled in the note', async () => {
    renderWithProviders(<CrashReportNotice />)
    await screen.findByRole('status')
    fireEvent.click(screen.getByRole('button', { name: /report issue/i }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent(/report a problem/i)
    const note = within(dialog).getByRole('textbox', { name: /what happened/i })
    expect((note as HTMLTextAreaElement).value).toMatch(/closed unexpectedly/i)
    expect((note as HTMLTextAreaElement).value).toBe(
      'The app closed unexpectedly. New crash records saved on this machine: 2.',
    )
    expect(reveal).not.toHaveBeenCalled()
  })

  it('keeps the crash banner when the report dialog is cancelled', async () => {
    renderWithProviders(<CrashReportNotice />)
    await screen.findByRole('status')
    fireEvent.click(screen.getByRole('button', { name: /report issue/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  it('dismisses for the rest of the session', async () => {
    renderWithProviders(<CrashReportNotice />)
    await screen.findByRole('status')
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  // The desktop shell refuses the ask while its port-owner check fails, which
  // can last minutes after launch, and runs no scan on a refused ask. The notice
  // asks again on a bounded schedule instead of waiting for a page reload.
  describe('re-asking after a refused ask', () => {
    beforeEach(() => { vi.useFakeTimers() })
    afterEach(() => { vi.useRealTimers() })

    it('announces the crash once a later ask is answered', async () => {
      get.mockRejectedValueOnce(refused()).mockRejectedValueOnce(refused())
      renderWithProviders(<CrashReportNotice />)
      await advance(0)
      expect(get).toHaveBeenCalledTimes(1)
      expect(screen.queryByRole('status')).not.toBeInTheDocument()
      await advance(15_000)
      expect(get).toHaveBeenCalledTimes(2)
      await advance(45_000)
      expect(get).toHaveBeenCalledTimes(3)
      expect(screen.getByRole('status')).toHaveTextContent(/closed unexpectedly/i)
      await advance(PAST_SCHEDULE_MS)
      expect(get).toHaveBeenCalledTimes(3)
    })

    it('stops after five re-asks when every ask is refused', async () => {
      get.mockRejectedValue(refused())
      renderWithProviders(<CrashReportNotice />)
      await advance(PAST_SCHEDULE_MS)
      expect(get).toHaveBeenCalledTimes(6)
      expect(screen.queryByRole('status')).not.toBeInTheDocument()
      await advance(PAST_SCHEDULE_MS)
      expect(get).toHaveBeenCalledTimes(6)
    })

    it('asks no more once an ask is answered, even with nothing new', async () => {
      get.mockResolvedValue(summary({ newCount: 0 }))
      renderWithProviders(<CrashReportNotice />)
      await advance(PAST_SCHEDULE_MS)
      expect(get).toHaveBeenCalledTimes(1)
      expect(screen.queryByRole('status')).not.toBeInTheDocument()
    })

    it('clears a pending re-ask on unmount', async () => {
      get.mockRejectedValue(refused())
      const { unmount } = renderWithProviders(<CrashReportNotice />)
      await advance(0)
      expect(get).toHaveBeenCalledTimes(1)
      unmount()
      await advance(PAST_SCHEDULE_MS)
      expect(get).toHaveBeenCalledTimes(1)
    })
  })
})
