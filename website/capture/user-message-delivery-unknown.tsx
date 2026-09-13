/**
 * Isolated capture entry for the delivery surfaces: the user bubble's UNKNOWN-DELIVERY
 * state, and every state of the composer's DeliveryWarningStrip.
 *
 * WHY ISOLATED: the state needs a transport failure AFTER the bytes may already
 * have left, so shooting it live means breaking the network mid-request. The row
 * is a pure function of `content` + `meta`, so handing it the meta the reducer
 * writes reaches the real render with no gateway and no timing. The strip is a
 * pure function of `delivered` + `discardable` for the same reason.
 *
 * TWO capture roots, shot separately: `data-capture-root` holds the bubbles and
 * `data-capture-strip` the strip, so each self-check counts only its own subject.
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
import { DeliveryWarningStrip } from '../src/components/DeliveryWarningStrip'
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

/** Every state the strip can render, so no exit label or the note goes unphotographed.
 *  `discardable` decides Discard/Clear vs Remove/Dismiss; `delivered` decides the caption
 *  and which of each pair applies. The note renders on exactly one of the four. */
const STRIPS: Array<{ label: string; delivered: boolean; discardable: boolean }> = [
  { label: 'unconfirmed + composer still holds the payload — Discard', delivered: false, discardable: true },
  { label: 'delivered + composer still holds the payload — Clear', delivered: true, discardable: true },
  { label: 'unconfirmed + composer has moved on — Remove, and the consequence note', delivered: false, discardable: false },
  { label: 'delivered + composer has moved on — Dismiss', delivered: true, discardable: false },
]

function StripScene() {
  return (
    <div data-capture-strip className="bg-bg p-5 flex flex-col gap-5" style={{ width: 720 }}>
      {STRIPS.map((s, i) => (
        <div key={i} className="flex flex-col gap-1.5">
          <div className="text-[11px] text-muted font-mono">{s.label}</div>
          <DeliveryWarningStrip
            noteId={`capture-note-${i}`}
            delivered={s.delivered}
            discardable={s.discardable}
            onDiscard={() => undefined}
            onDismiss={() => undefined}
          />
        </div>
      ))}
    </div>
  )
}

await initI18n()
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={new QueryClient()}>
    <MemoryRouter>
      <>
        <Scene />
        <StripScene />
      </>
    </MemoryRouter>
  </QueryClientProvider>,
)
