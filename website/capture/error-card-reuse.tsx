/**
 * Evidence for #6209 — the app-sdk `error` registry entry now renders the
 * shared ErrorCard instead of a hand-rolled div that duplicated its recipe
 * class-for-class.
 *
 * PIXEL-IDENTICAL IS THE INTENDED OUTCOME. This is a duplication removal, not
 * a restyle: the before and after frames are supposed to be indistinguishable,
 * and the capture exists to PROVE the no-op rather than to show a new look.
 *
 * BEFORE uses the retired hand-rolled recipe as a literal class string, FROZEN
 * at the commit that retired it (the #6209 fix) — it deliberately does not
 * track later edits to src/. Unlike the notice-card capture (which had to
 * replicate with inline styles because its old classes left src/ entirely),
 * every class here still lives in src/pages/chat/ErrorCard.tsx's
 * non-continuable branch, so Tailwind compiles them and the replica renders
 * exactly as the old entry did at that commit. If ErrorCard's settled branch
 * is ever restyled, this sheet documents the historical no-op and should not
 * be re-captured against the new look.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import { ErrorCard } from '../src/pages/chat/ErrorCard'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(params.get('lang') || 'en')

/** A representative gateway-authored error row. */
const ERROR_TEXT = '⟳ Connection lost — please retry.'

/** The old app-sdk `error` entry, verbatim (classes still compiled via ErrorCard). */
function OldErrorRow({ content }: { content: string }) {
  return (
    <div className="bg-danger-subtle text-danger text-[13px] leading-5 px-3 py-2 rounded-md ring-1 ring-inset forced-colors:border ring-danger/15 self-center animate-scale-in">
      {content}
    </div>
  )
}

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

function Scene() {
  return (
    <div
      data-capture-root
      style={{
        maxWidth: 720,
        margin: '0 auto',
        padding: '20px 24px 28px',
        background: 'var(--bg)',
        color: 'var(--text)',
      }}
    >
      <Label>BEFORE — hand-rolled div in the app-sdk registry</Label>
      <div data-episode="before" style={{ display: 'flex', flexDirection: 'column' }}>
        <OldErrorRow content={ERROR_TEXT} />
      </div>
      <Label>AFTER — shared ErrorCard (no onContinue → settled shape)</Label>
      <div data-episode="after" style={{ display: 'flex', flexDirection: 'column' }}>
        <ErrorCard content={ERROR_TEXT} />
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Scene />)
