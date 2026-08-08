import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import RunInTerminalBtn from '../components/RunInTerminalBtn'

// "Run in terminal" dispatches a `mc:run-in-terminal` request on window;
// ChatPage opens a terminal tab in the active chat, runs it, and replies with a
// `mc:run-in-terminal-result`. Tests capture requests and simulate the reply.
//
// A click does not run anything on its own — it opens a confirmation dialog
// showing the exact command, and only the dialog's Run button dispatches.
let requests: { code: string; reqId: string }[] = []
function onReq(e: Event) { requests.push((e as CustomEvent).detail) }
function replyLast(ok: boolean) {
  const last = requests[requests.length - 1]
  window.dispatchEvent(new CustomEvent('mc:run-in-terminal-result', { detail: { reqId: last.reqId, ok } }))
}

/** Click the trigger, then confirm in the dialog. */
function clickAndConfirm() {
  fireEvent.click(screen.getByRole('button', { name: 'Run in terminal' }))
  fireEvent.click(screen.getByRole('button', { name: /^Run( anyway)?$/ }))
}

beforeEach(() => {
  requests = []
  window.addEventListener('mc:run-in-terminal', onReq)
  vi.useFakeTimers()
})

afterEach(() => {
  window.removeEventListener('mc:run-in-terminal', onReq)
  vi.useRealTimers()
})

describe('RunInTerminalBtn', () => {
  it('renders terminal icon button', () => {
    renderWithProviders(<RunInTerminalBtn code="ls -la" />)
    expect(screen.getByRole('button', { name: 'Run in terminal' })).toBeInTheDocument()
    expect(screen.getByTitle('Run in terminal')).toBeInTheDocument()
  })

  it('opens a confirmation dialog instead of running on click', () => {
    renderWithProviders(<RunInTerminalBtn code="echo hello" />)
    fireEvent.click(screen.getByRole('button', { name: 'Run in terminal' }))
    expect(requests).toHaveLength(0)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run' })).toBeInTheDocument()
  })

  it('shows the full command in the dialog, including text the code block would clip', () => {
    const long = `echo ${'x'.repeat(400)} && rm -rf build`
    renderWithProviders(<RunInTerminalBtn code={long} />)
    fireEvent.click(screen.getByRole('button', { name: 'Run in terminal' }))
    expect(screen.getByRole('dialog').textContent).toContain('&& rm -rf build')
  })

  it('requests a run with the code once confirmed', () => {
    renderWithProviders(<RunInTerminalBtn code="echo hello" />)
    clickAndConfirm()
    expect(requests).toHaveLength(1)
    expect(requests[0].code).toBe('echo hello')
  })

  it('does not run when the dialog is cancelled', () => {
    renderWithProviders(<RunInTerminalBtn code="echo hello" />)
    fireEvent.click(screen.getByRole('button', { name: 'Run in terminal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(requests).toHaveLength(0)
    expect(screen.getByRole('button', { name: 'Run in terminal' })).toBeInTheDocument()
  })

  it('does not run when the dialog is dismissed with Escape', () => {
    renderWithProviders(<RunInTerminalBtn code="echo hello" />)
    fireEvent.click(screen.getByRole('button', { name: 'Run in terminal' }))
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(requests).toHaveLength(0)
  })

  it('strips prompt characters before requesting', () => {
    renderWithProviders(<RunInTerminalBtn code="$ git status" />)
    clickAndConfirm()
    expect(requests[0].code).toBe('git status')
  })

  it('previews the stripped command, not the raw prompt text', () => {
    renderWithProviders(<RunInTerminalBtn code="$ git status" />)
    fireEvent.click(screen.getByRole('button', { name: 'Run in terminal' }))
    expect(screen.getByRole('dialog').textContent).toContain('git status')
    expect(screen.getByRole('dialog').textContent).not.toContain('$ git status')
  })

  it('strips prompt chars from multiline code', () => {
    renderWithProviders(<RunInTerminalBtn code={"$ cd /tmp\n$ ls"} />)
    clickAndConfirm()
    expect(requests[0].code).toBe('cd /tmp\nls')
  })

  it('shows check icon after a successful result', () => {
    renderWithProviders(<RunInTerminalBtn code="ls" />)
    clickAndConfirm()
    act(() => { replyLast(true) })
    expect(screen.getByLabelText('Sent to terminal')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Run in terminal' })).not.toBeInTheDocument()
  })

  it('closes the dialog once confirmed', async () => {
    // Real timers: the modal exit is a framer-motion animation, so the node
    // lingers for a frame after confirm.
    vi.useRealTimers()
    renderWithProviders(<RunInTerminalBtn code="ls" />)
    clickAndConfirm()
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Run' })).not.toBeInTheDocument())
  })

  it('reverts to idle after the success flash timeout', () => {
    renderWithProviders(<RunInTerminalBtn code="ls" />)
    clickAndConfirm()
    act(() => { replyLast(true) })
    expect(screen.getByLabelText('Sent to terminal')).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(1200) })
    expect(screen.getByRole('button', { name: 'Run in terminal' })).toBeInTheDocument()
  })

  it('shows error on a failed result', () => {
    renderWithProviders(<RunInTerminalBtn code="ls" />)
    clickAndConfirm()
    act(() => { replyLast(false) })
    expect(screen.getByLabelText("Couldn't run in terminal")).toBeInTheDocument()
  })

  it('shows error when no result arrives (timeout)', () => {
    renderWithProviders(<RunInTerminalBtn code="ls" />)
    clickAndConfirm()
    act(() => { vi.advanceTimersByTime(8000) })
    expect(screen.getByLabelText("Couldn't run in terminal")).toBeInTheDocument()
  })

  it('reverts from error state after timeout', () => {
    renderWithProviders(<RunInTerminalBtn code="ls" />)
    clickAndConfirm()
    act(() => { replyLast(false) })
    expect(screen.getByLabelText("Couldn't run in terminal")).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(2000) })
    expect(screen.getByRole('button', { name: 'Run in terminal' })).toBeInTheDocument()
  })

  it('does not strip $ when not followed by whitespace (variable ref)', () => {
    renderWithProviders(<RunInTerminalBtn code={"$HOME/bin/run"} />)
    clickAndConfirm()
    expect(requests[0].code).toBe('$HOME/bin/run')
  })

  it('does not strip $(subshell) syntax', () => {
    renderWithProviders(<RunInTerminalBtn code={"$(whoami)"} />)
    clickAndConfirm()
    expect(requests[0].code).toBe('$(whoami)')
  })

  it('does nothing when code is empty after stripping prompt chars', () => {
    renderWithProviders(<RunInTerminalBtn code="$ " />)
    fireEvent.click(screen.getByRole('button', { name: 'Run in terminal' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(requests).toHaveLength(0)
  })
})
