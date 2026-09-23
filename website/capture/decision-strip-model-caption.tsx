/**
 * Evidence for the routing receipt after the baseline caption is dropped.
 *
 * The collapsed row carries the DECISION and nothing else: which tier this turn
 * was put in, which model that landed on, the score and the elapsed time. The
 * model the turn would OTHERWISE have used is not a second fact on a routed
 * session — it is the previous turn's tier, so a `default: …` caption there named
 * a "default" that changed every turn. It is still on the record, one click in,
 * labelled `Model without routing`.
 *
 * Both rows mount the REAL DecisionStrip and read the REAL gateway record shape
 * through `readModelRecord()`, so the frame differs from production only in the
 * fixture. The second row is the same record with the panel expanded, which is
 * where the baseline now lives.
 *
 *   ?theme=dark|light
 */
import { useEffect } from 'react'
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

/** A routed turn as the gateway stamps it: `complex` answered, the tier's pin applied. */
const WIRE = {
  turn_id: 'tm-capture-1',
  ts: '2026-09-22T17:00:00Z',
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

const RECORD = readModelRecord(WIRE)!

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

/** Opens the panel through the row's own toggle, so nothing here bypasses the component. */
function Expanded() {
  useEffect(() => {
    const scope = document.querySelector('[data-episode="expanded"]')
    scope?.querySelector<HTMLButtonElement>('[data-testid="decision-strip-model-toggle"]')?.click()
  }, [])
  return (
    <div data-episode="expanded">
      <DecisionStrip record={RECORD} />
    </div>
  )
}

function Scene() {
  return (
    <div
      data-capture-root
      className="bg-bg text-text"
      style={{ maxWidth: 760, margin: '0 auto', padding: '20px 24px 28px' }}
    >
      <Label>COLLAPSED — tier, the model it landed on, score, latency. No baseline caption.</Label>
      <div data-episode="collapsed">
        <DecisionStrip record={RECORD} />
      </div>
      <Label>EXPANDED — the same record, where the baseline is named for what it is.</Label>
      <Expanded />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <MemoryRouter>
      <Scene />
    </MemoryRouter>
  </QueryClientProvider>,
)
