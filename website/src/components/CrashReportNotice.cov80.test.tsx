import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import CrashReportNotice from './CrashReportNotice'

type Summary = { newCount: number }

const get = vi.fn<[], Promise<Summary>>()
const reveal = vi.fn<[], Promise<{ ok: boolean; error?: string }>>()

/** Installs (or removes) the preload bridge the component reads off `window`. */
function bridge(present: boolean) {
  const w = window as unknown as { crashReportsAPI?: unknown }
  if (present) w.crashReportsAPI = { get, reveal }
  else delete w.crashReportsAPI
}

function summary(over: Partial<Summary> = {}): Summary {
  return { newCount: 2, ...over }
}

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

  it('dismisses for the rest of the session', async () => {
    renderWithProviders(<CrashReportNotice />)
    await screen.findByRole('status')
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})
