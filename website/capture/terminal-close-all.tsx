/**
 * Isolated capture entry for the dock terminal's overflow menu with its
 * Close all terminals controls, and the confirm they raise.
 *
 * The REAL `TerminalTabsView` holds three tabs. A stand-in WebSocket opens
 * each shell and reports its title: the second runs `npm run dev` (busy), the
 * others sit at a prompt.
 *
 * The page follows the app's own theme setting.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { ThemeProvider } from '../src/hooks/useTheme'
import { TerminalTabsView } from '../src/components/BottomTerminalPanel'
import { addTab, openBottomTerminal } from '../src/hooks/useBottomTerminal'
import '../src/index.css'


let dialed = 0
class TitledSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  readyState = TitledSocket.CONNECTING
  binaryType = 'arraybuffer'
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string | ArrayBuffer }) => void) | null = null
  onclose: ((e: { code: number }) => void) | null = null
  onerror: (() => void) | null = null
  constructor() {
    const n = dialed++
    setTimeout(() => {
      this.readyState = TitledSocket.OPEN
      this.onopen?.()
      this.onmessage?.({ data: JSON.stringify({ type: 'ready', shell: '/bin/bash' }) })
      const busy = n === 1
      this.onmessage?.({ data: JSON.stringify({ type: 'title', text: busy ? 'npm' : 'project' }) })
      this.onmessage?.({ data: new TextEncoder().encode(busy ? '$ npm run dev\r\n  ready on 127.0.0.1:5173\r\n' : '$ ').buffer })
    }, 50)
  }
  send() {}
  close() { this.readyState = TitledSocket.CLOSED }
}
;(window as unknown as { WebSocket: unknown }).WebSocket = TitledSocket
const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/')) return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } })
  return realFetch(input, init)
}

initI18n('en')
localStorage.clear()
openBottomTerminal('/home/user/project')
addTab('/home/user/project')
addTab('/home/user/project')
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <Provider store={store}>
      <ThemeProvider>
        <MemoryRouter>
          <div className="bg-bg text-text" style={{ width: '100%', height: '100vh', display: 'flex', flexDirection: 'column', justifyContent: 'flex-end' }}>
            <div className="h-[300px] border-t border-border flex flex-col" data-capture-pane>
              <TerminalTabsView variant="dock" />
            </div>
          </div>
        </MemoryRouter>
      </ThemeProvider>
    </Provider>
  </QueryClientProvider>,
)
