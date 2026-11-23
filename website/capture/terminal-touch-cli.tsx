/**
 * Isolated capture entry for touch scrolling in the REAL CliPanel.
 *
 * A stand-in WebSocket opens, sends `ready`, then the output of one scene:
 *   ?scene=shell  -- `seq 500` at a prompt: plain scrollback
 *   ?scene=alt    -- a full-screen pager on the alternate screen (no scrollback)
 *   ?scene=mouse  -- the same pager with mouse tracking on (a TUI)
 * Bytes the panel sends back are recorded on `window.__sent`, so a driver can
 * see wheel or arrow input reach the shell.
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n'
import CliPanel from '../src/components/CliPanel'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'shell'
document.documentElement.setAttribute('data-theme', 'kiro-dark')

function output(): string {
  const seq = Array.from({ length: 500 }, (_, i) => String(i + 1)).join('\r\n')
  if (scene === 'shell') return `$ seq 500\r\n${seq}\r\n$ `
  const page = Array.from({ length: 60 }, (_, i) => `line ${i + 1} of the pager`).join('\r\n')
  const mouse = scene === 'mouse' ? '\x1b[?1000h\x1b[?1006h' : ''
  return `\x1b[?1049h${mouse}\x1b[H${page}`
}

const sent: string[] = []
;(window as unknown as { __sent: string[] }).__sent = sent

class ShellSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  readyState = ShellSocket.CONNECTING
  binaryType = 'arraybuffer'
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string | ArrayBuffer }) => void) | null = null
  onclose: ((e: { code: number }) => void) | null = null
  onerror: (() => void) | null = null
  constructor() {
    setTimeout(() => {
      this.readyState = ShellSocket.OPEN
      this.onopen?.()
      this.onmessage?.({ data: JSON.stringify({ type: 'ready', shell: '/bin/bash' }) })
      this.onmessage?.({ data: new TextEncoder().encode(output()).buffer })
    }, 50)
  }
  send(data: unknown) {
    if (data instanceof Uint8Array) sent.push(new TextDecoder().decode(data))
    else if (typeof data === 'string') sent.push(data)
  }
  close() { this.readyState = ShellSocket.CLOSED }
}
;(window as unknown as { WebSocket: unknown }).WebSocket = ShellSocket
const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/')) return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } })
  return realFetch(input, init)
}

initI18n('en')
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <MemoryRouter>
      <div className="bg-bg text-text" style={{ width: '100%', height: '100vh', display: 'flex', flexDirection: 'column' }}>
        <div className="flex-1 min-h-0 flex flex-col" data-capture-pane>
          <CliPanel sessionId="capture-touch" cwd="/home/user/project" />
        </div>
      </div>
    </MemoryRouter>
  </QueryClientProvider>,
)
