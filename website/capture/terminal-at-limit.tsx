/**
 * Isolated capture entry for a terminal tab the gateway refused at its
 * terminal ceiling (`terminal_max_sessions`).
 *
 * The REAL `CliPanel` runs against a stand-in WebSocket that opens and answers
 * with the gateway's coded refusal, so the banner is the one production shows:
 * the reason and Reconnect.
 *
 * Query string: ?theme=dark|light.
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n'
import CliPanel from '../src/components/CliPanel'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** Every dial meets the same refusal: open, the coded error frame, close. */
class RefusingSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  readyState = RefusingSocket.CONNECTING
  binaryType = 'arraybuffer'
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: ((e: { code: number }) => void) | null = null
  onerror: (() => void) | null = null
  constructor() {
    setTimeout(() => {
      this.readyState = RefusingSocket.OPEN
      this.onopen?.()
      this.onmessage?.({ data: JSON.stringify({ type: 'error', message: 'Max 12 terminal sessions', code: 'terminal_max_sessions' }) })
      this.readyState = RefusingSocket.CLOSED
      this.onclose?.({ code: 1000 })
    }, 50)
  }
  send() {}
  close() { this.readyState = RefusingSocket.CLOSED }
}
;(window as unknown as { WebSocket: unknown }).WebSocket = RefusingSocket

initI18n('en')
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <MemoryRouter>
      <div className="bg-bg text-text p-4" style={{ width: '100%', height: '100vh' }}>
        <div className="h-[320px] rounded-md border border-border overflow-hidden flex flex-col" data-capture-pane>
          <CliPanel sessionId="capture-at-limit" cwd="/home/user/project" />
        </div>
      </div>
    </MemoryRouter>
  </QueryClientProvider>,
)
