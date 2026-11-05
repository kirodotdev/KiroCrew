/**
 * Capture entry for the Session storage screen's refusal notice: the REAL
 * `SessionStorageScreen` against a fixed inventory, where a Move to Trash comes
 * back with one or several sessions the server refused.
 *
 * `fetch` is stubbed for the four session-storage routes the screen reads, so no
 * gateway runs. The capture script selects rows and presses Move to Trash.
 * Query string: ?theme=dark|light&refused=one|several
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n'
import SessionStorageScreen from '../src/pages/system/SessionStorageScreen'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const several = params.get('refused') === 'several'

localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
document.documentElement.setAttribute('data-mode', theme)

const NOW = Date.UTC(2026, 9, 29, 12) / 1000

const INVENTORY = {
  total_bytes: 27_400_000_000,
  total_sessions: 612,
  reclaimable_bytes: 24_100_000_000,
  reclaim_blocked_reason: '',
  sessions: [
    { uid: 'dashboard_chat-70', title: 'Refactor the ACP adapter', origin: 'dashboard · chat-70', bytes: 3_810_000_000, mtime: NOW - 86_400 * 40, active: false, live: false, background: false },
    { uid: 'dashboard_chat-52', title: 'Sydney property platform', origin: 'dashboard · chat-52', bytes: 536_000_000, mtime: NOW - 3_600, active: false, live: false, background: false },
    { uid: 'dashboard_chat-61', title: 'Release notes draft', origin: 'dashboard · chat-61', bytes: 210_000_000, mtime: NOW - 86_400 * 12, active: false, live: false, background: false },
  ],
  background: { sessions: 0, bytes: 0, listed: 0 },
  age_options: [
    { days: 7, sessions: 480, bytes: 24_000_000_000 },
    { days: 30, sessions: 300, bytes: 18_000_000_000 },
  ],
  trash: { bytes: 0, still_on_disk: true, instant: true, batches: [] },
}

const REFUSED = several
  ? [
      { uid: 'dashboard_chat-70', reason: 'in_use' },
      { uid: 'dashboard_chat-52', reason: 'too_fresh' },
    ]
  : [{ uid: 'dashboard_chat-70', reason: 'in_use' }]

const json = (body: unknown) =>
  Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }))

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/system/session-storage/trash')) {
    return json({ sessions: 0, bytes: 0, batch_id: '', refused: REFUSED })
  }
  if (url.includes('/api/system/session-storage/empty')) return json({ job: null })
  if (url.includes('/api/system/session-storage/sessions/')) {
    return json({ uid: '', files: [], total_bytes: 0 })
  }
  if (url.includes('/api/system/session-storage/sessions')) return json(INVENTORY)
  if (url.includes('/api/')) return json({})
  return realFetch(input, init)
}) as typeof globalThis.fetch

initI18n('en')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <MemoryRouter>
      <div className="bg-bg text-text min-h-screen p-4" style={{ maxWidth: 820 }}>
        <SessionStorageScreen onBack={() => {}} />
      </div>
    </MemoryRouter>
  </QueryClientProvider>,
)
