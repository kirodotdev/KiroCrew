/**
 * Evidence for the routed effort on the model-routing receipt.
 *
 * Mounts the REAL DecisionStrip over the REAL gateway record shape read through
 * `readModelRecord()`. The first row is a turn whose tier routed the effort, so the
 * collapsed line names Jev's level beside the owner's; the second is the same turn
 * with no routed effort, which draws none.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { queryClient } from '../src/api/queryClient'
import { initI18n } from '../src/i18n/all'
import DecisionStrip from '../src/pages/chat/DecisionStrip'
import { readModelRecord } from '../src/pages/chat/decisionRecord'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(params.get('lang') || 'en')

const WIRE = {
  turn_id: 'tm-capture-effort',
  ts: '2026-10-24T09:00:00Z',
  point: 'model.route',
  session: 'abc123def456',
  tier: 'complex',
  model_chosen: 'gpt-5.6-terra',
  baseline_model: 'gpt-5.6-sol',
  p: 0.91,
  latency_ms: 180,
  history_chars: 0,
  truncated: 0,
  scrubbed: false,
  answers: null,
  error: null,
}

const ROUTED = readModelRecord({ ...WIRE, effort_chosen: 'high', baseline_effort: 'medium' })!
const PLAIN = readModelRecord({ ...WIRE, turn_id: 'tm-capture-plain' })!

function Label({ children }: { children: string }) {
  return (
    <div
      style={{
        fontSize: 11,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        opacity: 0.55,
        margin: '18px 0 6px',
        fontFamily: 'ui-sans-serif, system-ui, sans-serif',
      }}
    >
      {children}
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <MemoryRouter>
      <div
        data-capture-root
        className="bg-bg text-text"
        style={{ maxWidth: 760, margin: '0 auto', padding: '20px 24px 28px' }}
      >
        <Label>ROUTED EFFORT — Jev's level beside the owner's.</Label>
        <DecisionStrip record={ROUTED} />
        <Label>NO ROUTED EFFORT — the same turn draws none.</Label>
        <DecisionStrip record={PLAIN} />
      </div>
    </MemoryRouter>
  </QueryClientProvider>,
)
