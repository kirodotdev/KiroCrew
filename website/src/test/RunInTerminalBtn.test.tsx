import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import RunInTerminalBtn from '../components/RunInTerminalBtn'

// "Run in terminal" now dispatches a `mc:run-in-terminal` request on window;
// ChatPage opens a terminal tab in the active chat, runs it, and replies with a
// `mc:run-in-terminal-result`. Tests capture requests and simulate the reply.
let requests: { code: string; reqId: string }[] = []
function onReq(e: Event) { requests.push((e as CustomEvent).detail) }
function replyLast(ok: boolean) {
  const last = requests[requests.length - 1]
  window.dispatchEvent(new CustomEvent('mc:run-in-terminal-result', { detail: { reqId: last.reqId, ok } }))
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
    expect(screen.getByLabelText('Run in terminal')).toBeInTheDocument()
    expect(screen.getByTitle('Run in terminal')).toBeInTheDocument()
  })

  it('requests a run with the code on click', () => {
    renderWithProviders(<RunInTerminalBtn code="echo hello" />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))
    expect(requests).toHaveLength(1)
    expect(requests[0].code).toBe('echo hello')
  })

  it('strips prompt characters before requesting', () => {
    renderWithProviders(<RunInTerminalBtn code="$ git status" />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))
    expect(requests[0].code).toBe('git status')
  })

  it('strips prompt chars from multiline code', () => {
    renderWithProviders(<RunInTerminalBtn code={"$ cd /tmp\n$ ls"} />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))
    expect(requests[0].code).toBe('cd /tmp\nls')
  })

  it('shows check icon after a successful result', () => {
    renderWithProviders(<RunInTerminalBtn code="ls" />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))
    act(() => { replyLast(true) })
    expect(screen.getByLabelText('Sent to terminal')).toBeInTheDocument()
    expect(screen.queryByLabelText('Run in terminal')).not.toBeInTheDocument()
  })

  it('reverts to idle after the success flash timeout', () => {
    renderWithProviders(<RunInTerminalBtn code="ls" />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))
    act(() => { replyLast(true) })
    expect(screen.getByLabelText('Sent to terminal')).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(1200) })
    expect(screen.getByLabelText('Run in terminal')).toBeInTheDocument()
  })

  it('shows error on a failed result', () => {
    renderWithProviders(<RunInTerminalBtn code="ls" />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))
    act(() => { replyLast(false) })
    expect(screen.getByLabelText("Couldn't run in terminal")).toBeInTheDocument()
  })

  it('shows error when no result arrives (timeout)', () => {
    renderWithProviders(<RunInTerminalBtn code="ls" />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))
    act(() => { vi.advanceTimersByTime(8000) })
    expect(screen.getByLabelText("Couldn't run in terminal")).toBeInTheDocument()
  })

  it('reverts from error state after timeout', () => {
    renderWithProviders(<RunInTerminalBtn code="ls" />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))
    act(() => { replyLast(false) })
    expect(screen.getByLabelText("Couldn't run in terminal")).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(2000) })
    expect(screen.getByLabelText('Run in terminal')).toBeInTheDocument()
  })

  it('does not strip $ when not followed by whitespace (variable ref)', () => {
    renderWithProviders(<RunInTerminalBtn code={"$HOME/bin/run"} />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))
    expect(requests[0].code).toBe('$HOME/bin/run')
  })

  it('does not strip $(subshell) syntax', () => {
    renderWithProviders(<RunInTerminalBtn code={"$(whoami)"} />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))
    expect(requests[0].code).toBe('$(whoami)')
  })

  it('does nothing when code is empty after stripping prompt chars', () => {
    renderWithProviders(<RunInTerminalBtn code="$ " />)
    fireEvent.click(screen.getByLabelText('Run in terminal'))
    expect(requests).toHaveLength(0)
  })
})
