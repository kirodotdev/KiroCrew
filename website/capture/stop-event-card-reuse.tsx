/**
 * Evidence for #6229 — the app-sdk `stop_event` registry entry now renders the
 * shared StopEventCard instead of a hand-rolled div that duplicated its
 * stopping/stopped recipe class-for-class.
 *
 * UNLIKE THE #6209 ErrorCard SHEET, THE FRAMES ARE NOT MEANT TO MATCH. That
 * capture proved a pixel-identical no-op; this one proves the opposite, because
 * the retired row rendered `message.content`, and a stop row's content is the
 * card's own JSON envelope — the gateway writes it into `cls` and mirrors it
 * into `content` for consumers that read only `content`
 * (dashboard/chat_handlers.py). So the old row printed
 * `{"kind":"stop_event","id":…,"state":…}` into the transcript for every state,
 * where the dashboard printed the stop's outcome.
 *
 * BEFORE uses the retired hand-rolled recipe as a literal class string, FROZEN
 * at the commit that retired it. Every class in it still lives in
 * src/pages/chat/StopEventCard.tsx's stopping/stopped branches, so Tailwind
 * compiles them and the replica renders exactly as the old entry did. If those
 * branches are ever restyled, this sheet documents the historical state and
 * should not be re-captured against the new look.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import StopEventCard from '../src/pages/chat/StopEventCard'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(params.get('lang') || 'en')

/** Exactly what the gateway writes for a stop, for each state it can settle in. */
function gatewayStopRow(state: string): ChatMessage {
  const data = {
    kind: 'stop_event',
    id: 'stop-4f8c1e2a9b',
    state,
    outcome: state === 'stopping' ? null : 'soft',
    ts_start: '2026-08-31T10:24:07.918+00:00',
  }
  const json = JSON.stringify(data)
  return { role: 'system', content: json, cls: json, meta: data } as unknown as ChatMessage
}

/** The old app-sdk `stop_event` entry, verbatim (classes still compiled via the card). */
function OldStopRow({ message }: { message: ChatMessage }) {
  return (
    <div className="text-danger text-[13px] leading-5 font-mono px-3 py-2 rounded-md bg-danger-subtle inline-flex items-center gap-2">
      {message.content}
    </div>
  )
}

const STATES = ['stopping', 'stopped', 'stop_failed_reset']

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

function StateTag({ children }: { children: string }) {
  return (
    <div
      style={{
        fontSize: 10,
        opacity: 0.4,
        margin: '8px 0 3px',
        fontFamily: 'ui-monospace, SFMono-Regular, monospace',
      }}
    >
      meta.state = {children}
    </div>
  )
}

function Scene() {
  return (
    <div
      data-capture-root
      style={{
        maxWidth: 900,
        margin: '0 auto',
        padding: '20px 24px 28px',
        background: 'var(--bg)',
        color: 'var(--text)',
      }}
    >
      <Label>BEFORE — hand-rolled div rendering content, which IS the envelope</Label>
      <div data-episode="before" style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start' }}>
        {STATES.map(s => (
          <div key={s} style={{ maxWidth: '100%', minWidth: 0 }}>
            <StateTag>{s}</StateTag>
            <div style={{ overflowWrap: 'anywhere' }}><OldStopRow message={gatewayStopRow(s)} /></div>
          </div>
        ))}
      </div>
      <Label>AFTER — shared StopEventCard, reading meta.state</Label>
      <div data-episode="after" style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start' }}>
        {STATES.map(s => (
          <div key={s}>
            <StateTag>{s}</StateTag>
            <StopEventCard message={gatewayStopRow(s)} />
          </div>
        ))}
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Scene />)
