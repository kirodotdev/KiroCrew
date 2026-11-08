/**
 * Isolated capture entry for the delivery-warning strip above the composer.
 *
 * WHY ISOLATED: the strip appears only after a send whose delivery is in doubt, which against a
 * live gateway means stalling the request that carries it. The strip takes no state of its own,
 * so rendering the real component against the real stylesheet and theme tokens shows exactly what
 * the user sees, with no gateway and no timing.
 *
 * Theme via query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n'
import { DeliveryWarningStrip } from '../src/components/DeliveryWarningStrip'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const ROWS: Array<{ label: string }> = [
  { label: 'delivery in doubt' },
]

function Scene() {
  return (
    <div data-capture-root className="bg-bg p-5 flex flex-col gap-5" style={{ width: 720 }}>
      {ROWS.map((row, i) => (
        <div key={i} data-capture-row className="flex flex-col gap-1.5">
          <div className="text-[11px] text-muted font-mono">{row.label}</div>
          <div className="rounded-lg border border-border bg-surface px-3 pb-2">
            <DeliveryWarningStrip
              noteId={`capture-note-${i}`}
              onDismiss={() => {}}
            />
            <div className="mt-2 rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-muted">
              Rebase this branch onto main and re-run the gates.
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

initI18n('en')

createRoot(document.getElementById('root')!).render(<Scene />)
