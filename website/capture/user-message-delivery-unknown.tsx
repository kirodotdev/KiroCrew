/**
 * Isolated capture entry for the user bubble's UNKNOWN-DELIVERY state.
 *
 * WHY ISOLATED: the state needs a transport failure AFTER the bytes may already
 * have left, so shooting it live means breaking the network mid-request. The row
 * is a pure function of `content` + `meta`, so handing it the meta the reducer
 * writes reaches the real render with no gateway and no timing.
 *
 * NOT the timeout-sweep indicator removed by #4180: that fired on a ~30s timer
 * with no evidence of failure, this only on an observed transport failure.
 *
 * Rows 1 and 3 are controls that must look identical in any checkout, so a
 * reviewer can attribute every difference to row 2. Theme: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n/all'
import UserMessage from '../src/pages/chat/UserMessage'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** The transcript renders user content as plain text; no markdown pass here. */
const renderContent = (content: string) => <>{content}</>

const ROWS: Array<{ label: string; content: string; meta: Record<string, unknown> }> = [
  {
    label: 'confirmed — echo reconciled, no marks (control, must not change)',
    content: 'Summarise the open work I should review today.',
    meta: { mid: 'm-1' },
  },
  {
    label: 'unknown delivery — `deliveryUnknown` (the state this PR adds)',
    content: 'Rebase this branch onto main and re-run the gates.',
    meta: { sendId: 's-2', pendingServerRow: true, deliveryUnknown: true },
  },
  {
    label: 'pending — retained, no failure observed (control, must not change)',
    content: 'Also check whether the Windows shard is still red.',
    meta: { sendId: 's-3', pendingServerRow: true },
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

await initI18n()
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={new QueryClient()}>
    <MemoryRouter>
      <Scene />
    </MemoryRouter>
  </QueryClientProvider>,
)
