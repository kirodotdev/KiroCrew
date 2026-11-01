import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderWithProviders } from './helpers'
import RunYourselfCard, { runYourselfSummary, RUN_YOURSELF_SUMMARY_MAX } from '../components/RunYourselfCard'
import EditableCodeBlock from '../components/EditableCodeBlock'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { setTerminalEnabledFlag } from '../utils/terminalRegistry'
import { RUN_IN_TERMINAL_RESULT_FALLBACK_MS } from '../utils/fenceShell'

// The card a crewmate shows for a command a safety rule stopped it from
// running (bug-bash H4). It must show the command as written, route Run
// through the same confirm dialog as a bash block, and fall back to Copy.

// Assembled at runtime so this file's own text is an ordinary line.
const INSTALL = ['curl', "--proto '=https'", '--tlsv1.2', '-sSf', 'https://sh.rustup.rs', '|', 'sh'].join(' ')

let requests: { code: string; reqId: string; lang?: string }[] = []
function onReq(e: Event) { requests.push((e as CustomEvent).detail) }
const writeText = vi.fn(async (_t: string) => {})

beforeEach(() => {
  requests = []
  window.addEventListener('mc:run-in-terminal', onReq)
  writeText.mockClear()
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  setTerminalEnabledFlag(true)
})

afterEach(() => {
  window.removeEventListener('mc:run-in-terminal', onReq)
  setTerminalEnabledFlag(false)
  vi.useRealTimers()
})

describe('RunYourselfCard', () => {
  it('shows the command verbatim and why the agent could not run it', () => {
    renderWithProviders(<RunYourselfCard code={INSTALL} />)
    const card = screen.getByTestId('run-yourself-card')
    expect(card.textContent).toContain(INSTALL)
    expect(within(card).getByText('Run this yourself')).toBeInTheDocument()
    expect(within(card).getByText("I can't run this, but you can.")).toBeInTheDocument()
  })

  it('opens the existing confirm dialog and runs only after the person confirms', () => {
    renderWithProviders(<RunYourselfCard code={INSTALL} />)
    fireEvent.click(screen.getByRole('button', { name: 'Run in terminal' }))
    expect(requests).toHaveLength(0)
    const dialog = screen.getByRole('dialog')
    expect(dialog.textContent).toContain(INSTALL)
    // The curl-into-shell shape is flagged by the dialog's own sensitive check.
    fireEvent.click(within(dialog).getByRole('button', { name: /^Run( anyway)?$/ }))
    expect(requests).toEqual([{ code: INSTALL, reqId: expect.any(String) }])
  })

  it('cancelling the dialog sends nothing', () => {
    renderWithProviders(<RunYourselfCard code={INSTALL} />)
    fireEvent.click(screen.getByRole('button', { name: 'Run in terminal' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
    expect(requests).toHaveLength(0)
  })

  it('offers Copy alone when the dashboard has no terminal', async () => {
    setTerminalEnabledFlag(false)
    renderWithProviders(<RunYourselfCard code={INSTALL} />)
    expect(screen.queryByRole('button', { name: 'Run in terminal' })).not.toBeInTheDocument()
    expect(screen.getByRole('status').textContent).toMatch(/Running commands isn't available here/)
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(INSTALL))
    expect(requests).toHaveLength(0)
  })

  it('carries no hint line beyond the title, the reason and the command', () => {
    renderWithProviders(<RunYourselfCard code={INSTALL} />)
    const card = screen.getByTestId('run-yourself-card')
    // Everything visible: title, one-sentence reason, the command, two buttons.
    expect(card.textContent).toBe(
      'Run this yourself' + "I can't run this, but you can." + INSTALL + 'Run in terminal' + 'Copy',
    )
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('says the terminal did not respond, drops Run and keeps Copy', () => {
    vi.useFakeTimers()
    renderWithProviders(<RunYourselfCard code={INSTALL} />)
    fireEvent.click(screen.getByRole('button', { name: 'Run in terminal' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: /^Run( anyway)?$/ }))
    act(() => { vi.advanceTimersByTime(RUN_IN_TERMINAL_RESULT_FALLBACK_MS + 1) })
    // A failed run is an error, so it goes through ErrorNotice with its hand-off.
    const notice = screen.getByTestId('run-yourself-run-unanswered')
    expect(notice).toHaveTextContent(/The terminal didn't respond/)
    expect(within(notice).getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Run in terminal' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
    // The run's own error flash clears after two seconds; the fallback must not.
    act(() => { vi.advanceTimersByTime(5000) })
    expect(screen.getByTestId('run-yourself-run-unanswered')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Run in terminal' })).not.toBeInTheDocument()
  })

  it('shows the agent summary above the reason, as plain text', () => {
    renderWithProviders(<RunYourselfCard code={INSTALL} summary="Installs Rust from rustup.rs." />)
    expect(screen.getByTestId('run-yourself-card').textContent).toBe(
      'Run this yourself' + 'Installs Rust from rustup.rs.' + "I can't run this, but you can." + INSTALL + 'Run in terminal' + 'Copy',
    )
  })

  it('reads the summary from the fence info string, plain and capped', () => {
    expect(runYourselfSummary('summary="Installs Rust from rustup.rs."')).toBe('Installs Rust from rustup.rs.')
    expect(runYourselfSummary(undefined)).toBeUndefined()
    expect(runYourselfSummary('title="x"')).toBeUndefined()
    expect(runYourselfSummary('summary="   "')).toBeUndefined()
    const long = runYourselfSummary(`summary="${'a'.repeat(300)}"`)!
    expect(long.length).toBe(RUN_YOURSELF_SUMMARY_MAX)
    expect(long.endsWith('…')).toBe(true)
  })

  it('draws the fence summary through MarkdownRenderer, never as markup', () => {
    const md = '```run-yourself summary="Installs <b>Rust</b> from rustup.rs."\n' + INSTALL + '\n```\n'
    renderWithProviders(<MarkdownRenderer content={md} runYourselfCards />)
    const line = screen.getByTestId('run-yourself-summary')
    expect(line.textContent).toBe('Installs <b>Rust</b> from rustup.rs.')
    expect(line.querySelector('b')).toBeNull()
  })

  it('closing either error clears it, and closing the run error brings Run back', () => {
    vi.useFakeTimers()
    renderWithProviders(<RunYourselfCard code={INSTALL} />)
    fireEvent.click(screen.getByRole('button', { name: 'Run in terminal' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: /^Run( anyway)?$/ }))
    act(() => { vi.advanceTimersByTime(RUN_IN_TERMINAL_RESULT_FALLBACK_MS + 1) })
    const notice = screen.getByTestId('run-yourself-run-unanswered')
    expect(within(notice).getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
    fireEvent.click(within(notice).getByRole('button', { name: /dismiss|close/i }))
    expect(screen.queryByTestId('run-yourself-run-unanswered')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run in terminal' })).toBeInTheDocument()
  })

  it('still says Copy in the dialog when Run in terminal copies', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(['kirocrewConfig'], { dashboard: { terminal: { reuse_current: true } } })
    renderWithProviders(<QueryClientProvider client={qc}><RunYourselfCard code={INSTALL} /></QueryClientProvider>)
    fireEvent.click(screen.getByRole('button', { name: 'Run in terminal' }))
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('says the terminal is unavailable only when the dashboard has none', () => {
    setTerminalEnabledFlag(false)
    renderWithProviders(<RunYourselfCard code={INSTALL} />)
    expect(screen.getByRole('status').textContent).toBe("Running commands isn't available here, so copy it into the Terminal app on your computer.")
  })

  it('leaves an explicit refusal to the host and keeps Run', () => {
    renderWithProviders(<RunYourselfCard code={INSTALL} />)
    fireEvent.click(screen.getByRole('button', { name: 'Run in terminal' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: /^Run( anyway)?$/ }))
    // A Session Environment chat or a pending cwd: the bridge shows its own
    // notice and answers ok:false.
    const reqId = requests[requests.length - 1].reqId
    act(() => { window.dispatchEvent(new CustomEvent('mc:run-in-terminal-result', { detail: { reqId, ok: false } })) })
    expect(screen.queryByTestId('run-yourself-run-unanswered')).not.toBeInTheDocument()
    expect(screen.queryByText(/Terminal app on your computer/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Run in terminal|Couldn't run in terminal/ })).toBeInTheDocument()
  })

  it('reports a failed copy instead of doing nothing', async () => {
    setTerminalEnabledFlag(false)
    writeText.mockRejectedValueOnce(new Error('denied'))
    // The legacy fallback fails too (jsdom has no execCommand of its own).
    Object.defineProperty(document, 'execCommand', { value: () => false, configurable: true })
    renderWithProviders(<RunYourselfCard code={INSTALL} />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    const notice = await screen.findByTestId('run-yourself-copy-failed')
    expect(notice).toHaveTextContent(/copy it by hand/)
    // The sanctioned hand-off of errors-use-error-notice.
    expect(within(notice).getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
    delete (document as { execCommand?: unknown }).execCommand
  })

  it('is what a closed ```run-yourself fence renders as in an assistant turn', () => {
    renderWithProviders(<EditableCodeBlock code={INSTALL} lang="run-yourself" complete={true} runYourselfCard />)
    expect(screen.getByTestId('run-yourself-card')).toBeInTheDocument()
  })

  it('stays a plain code block on any other surface', () => {
    renderWithProviders(<EditableCodeBlock code={INSTALL} lang="run-yourself" complete={true} />)
    expect(screen.queryByTestId('run-yourself-card')).not.toBeInTheDocument()
  })

  it('is drawn by MarkdownRenderer only when the host opts in', () => {
    const md = '```run-yourself\n' + INSTALL + '\n```\n'
    const { unmount } = renderWithProviders(<MarkdownRenderer content={md} />)
    expect(screen.queryByTestId('run-yourself-card')).not.toBeInTheDocument()
    unmount()
    renderWithProviders(<MarkdownRenderer content={md} runYourselfCards />)
    expect(screen.getByTestId('run-yourself-card')).toBeInTheDocument()
  })

  it('never draws a fence that did not close as a runnable card', () => {
    // A stopped reply or a missing closer: the body may be a cut-off command.
    const md = '```run-yourself summary="Installs Rust."\n' + INSTALL + '\nand the rest of the reply'
    renderWithProviders(<MarkdownRenderer content={md} runYourselfCards />)
    expect(screen.queryByTestId('run-yourself-card')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Run in terminal' })).not.toBeInTheDocument()
  })

  it('streams as a plain block until the fence closes', () => {
    renderWithProviders(<EditableCodeBlock code="curl" lang="run-yourself" complete={false} runYourselfCard />)
    expect(screen.queryByTestId('run-yourself-card')).not.toBeInTheDocument()
  })
})
