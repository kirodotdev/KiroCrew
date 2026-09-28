/**
 * Isolated capture entry for the user chat bubble's unconfirmed state.
 *
 * WHY ISOLATED: the state this PR removes was reachable only ~30-40s after a
 * send whose server echo never arrived, so photographing it against a live
 * gateway means artificially stalling the WebSocket for half a minute. The row
 * itself is a pure function of `content` + `meta`, so handing it the exact meta
 * the reducer used to produce renders the real component — real classes, real
 * Tailwind output, real theme tokens — with no gateway and no timing.
 *
 * The same file renders in both checkouts, which is what makes the pair honest:
 * on the base commit the middle row draws the indicator, on this branch it does
 * not, and nothing about the harness differs between the two shots.
 *
 * Three rows cover what a reviewer needs to compare:
 *   1. a confirmed bubble (server echo reconciled, `optimistic` stripped)
 *   2. the state under test (`optimistic` + `stale`, the removed indicator)
 *   3. a pending bubble that has NOT timed out (`optimistic`, no `stale`)
 *
 * Rows 1 and 3 are the control: they must look identical in both shots, so any
 * difference the reviewer sees is attributable to row 2 alone.
 *
 * Theme via query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n'
import UserMessage from '../src/pages/chat/UserMessage'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** The transcript renders user content as plain text; no markdown pass here. */
const renderContent = (content: string) => <>{content}</>

const ROWS: Array<{ label: string; content: string; meta: Record<string, unknown> }> = [
  {
    label: 'confirmed — server echo reconciled, `optimistic` stripped',
    content: 'Summarise the open work I should review today.',
    meta: { mid: 'm-1' },
  },
  {
    label: 'unconfirmed — `optimistic` + `stale` (the state this PR removes)',
    content: 'Rebase this branch onto main and re-run the gates.',
    meta: { sendId: 's-2', optimistic: true, stale: true },
  },
  {
    label: 'pending — `optimistic`, not yet timed out (must not change)',
    content: 'Also check whether the Windows shard is still red.',
    meta: { sendId: 's-3', optimistic: true },
  },
]

function Scene() {
  return (
    <div data-capture-root className="bg-bg p-5 flex flex-col gap-5" style={{ width: 720 }}>
      {ROWS.map((row, i) => (
        <div key={i} className="flex flex-col gap-1.5">
          <div className="text-[11px] text-muted font-mono">{row.label}</div>
          <div className="flex flex-col items-end group/msg">
            <UserMessage
              content={row.content}
              meta={row.meta}
              timestamp="10:04"
              renderContent={renderContent}
            />
          </div>
        </div>
      ))}
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <Scene />
    </QueryClientProvider>
  </MemoryRouter>,
)
