/**
 * Evidence for terminal copy-on-select (Part of #20929).
 *
 * THE STATE: with Settings → Display → Terminal → Copy on select turned on,
 * ending a mouse drag in the built-in terminal writes the selection to the
 * clipboard and the selection toolbar opens already showing ✓ Copied, with
 * Send to chat still one click away. With it off, the toolbar opens on Copy.
 *
 * This mounts the REAL CliPanel against the REAL stylesheet, theme tokens and
 * live i18n catalog. Only the terminal socket is faked: a stand-in WebSocket
 * that opens, says `ready`, and writes a few lines of shell output, the same
 * frames the gateway's PTY socket sends. The preference is set through the
 * real `mc-terminal-font` store before the panel mounts.
 *
 * Query string: ?theme=dark|light&copy=on|off
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
localStorage.setItem('mc-terminal-font', JSON.stringify({
  fontFamily: '', fontSize: 13, cursorStyle: 'block', copyOnSelect: params.get('copy') !== 'off',
}))

const OUTPUT = [
  '$ kubectl get pods -n web',
  'NAME                         READY   STATUS    RESTARTS   AGE',
  'api-7d9f8c6b5-x2kqp          1/1     Running   0          3d',
  'worker-5b8c9d7f4-m8tzn       1/1     Running   2          3d',
  '$ ',
].join('\r\n')

class FakeTerminalSocket {
  static readonly OPEN = 1
  readyState = 0
  binaryType = 'arraybuffer'
  onopen: (() => void) | null = null
  onmessage: ((ev: { data: unknown }) => void) | null = null
  onclose: ((ev: { code: number }) => void) | null = null
  onerror: (() => void) | null = null
  constructor(public url: string) {
    setTimeout(() => {
      this.readyState = 1
      this.onopen?.()
      this.onmessage?.({ data: JSON.stringify({ type: 'ready', shell: 'bash' }) })
      this.onmessage?.({ data: new TextEncoder().encode(OUTPUT).buffer })
    }, 50)
  }
  send() {}
  close() { this.readyState = 3 }
  addEventListener() {}
  removeEventListener() {}
}
;(window as unknown as { WebSocket: unknown }).WebSocket = FakeTerminalSocket

await initI18n()
const { default: CliPanel } = await import('../src/components/CliPanel')

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={new QueryClient()}>
    <div data-capture-root style={{ width: 720, height: 240 }} className="bg-bg">
      <CliPanel sessionId="capture-term" onSendToChat={() => {}} />
    </div>
  </QueryClientProvider>,
)
