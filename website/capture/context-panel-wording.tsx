/**
 * Isolated capture entry for the Context panel's wording (#15520).
 *
 * WHY ISOLATED: each variant is a payload shape -- recording switched off, a
 * session-start row next to a later context rebuild, an earlier-turns range with
 * rows listed above it, a single earlier turn -- and a live gateway produces one of
 * them at a time, only after many turns. The REAL `ContextBreakdownPanel` renders
 * here from fabricated traces, one scene per variant, nothing stubbed but the data.
 *
 * Each scene is a `[data-scene]` box the capture script photographs on its own.
 * Theme: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import { ContextBreakdownPanel, type ContextTrace, type ContextTurn } from '../src/pages/ContextBreakdownPanel'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const START = { your_message: 120, memory: 30_000, lessons: 6_000, agent_instructions: 2_000, skill_index: 280 }
const TURN = { your_message: 800, critical_rules: 4200, lessons: 3100, memory: 1800, surface: 300 }

function turn(ordinal: number, phase: string, blocks: Record<string, number>): ContextTurn {
  return {
    ts: `2026-10-06T10:${String(ordinal % 60).padStart(2, '0')}:00Z`,
    phase,
    blocks,
    total_chars: Object.values(blocks).reduce((a, b) => a + b, 0),
    context_used: 20_000,
    context_window: 200_000,
    model: 'claude-opus',
    ordinal,
  }
}

function trace(turns: ContextTurn[], extra: Partial<ContextTrace> = {}): ContextTrace {
  return {
    slot: 'chat-demo', turns, totals: {}, injected_chars: 0, user_chars: 0,
    peak_context_used: 0, context_window: 200_000, window_days: 14, ...extra,
  }
}

const regular = (from: number, to: number) =>
  Array.from({ length: to - from + 1 }, (_, i) => turn(from + i, 'per_turn', { ...TURN, your_message: 600 + ((i * 137) % 900) }))

const SCENES: { id: string; label: string; trace: ContextTrace }[] = [
  { id: 'off', label: 'Recording switched off (empty)', trace: trace([], { recording: false }) },
  { id: 'yet', label: 'Recording on, no turn yet (empty)', trace: trace([], { recording: true }) },
  {
    id: 'start-and-rebuild',
    label: 'Turn 1 session start + a rebuild at turn 4',
    trace: trace([turn(1, 'session_start', START), ...regular(2, 3), turn(4, 'session_start', START), ...regular(5, 7)]),
  },
  {
    id: 'range-with-rebuild',
    label: 'Earlier range with a rebuild row listed above (turn 41, chart from 46)',
    trace: trace([turn(41, 'session_start', START), ...regular(46, 52)]),
  },
  { id: 'range', label: 'Earlier range, nothing listed above', trace: trace(regular(8, 12)) },
  { id: 'single', label: 'Exactly one earlier turn', trace: trace(regular(2, 5)) },
]

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <div style={{ background: 'var(--bg)', color: 'var(--text)', padding: 16, display: 'grid', gap: 24 }}>
    {SCENES.map(s => (
      <div key={s.id} data-scene={s.id} style={{ width: 460, padding: 8 }}>
        <div className="font-mono text-[11px] text-muted mb-2">{s.label}</div>
        <ContextBreakdownPanel trace={s.trace} chartWidth={420} />
      </div>
    ))}
  </div>,
)
