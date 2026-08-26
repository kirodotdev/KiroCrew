/**
 * Evidence for unifying the "notice" row style with RecoveryCard.
 *
 * THE PROBLEM. A gateway "notice" row (empty-response self-heal, model
 * fallback) rendered as a plain text box with no icon slot, a severity emoji
 * baked into the message text sitting off-baseline, and shrink-to-content
 * width — while the RecoveryCard that always lands right beside it (an
 * empty-response episode emits both) is a full-width card with a 13px lucide
 * icon in a gap-2 leading slot. Two rows, one episode, two unrelated looks.
 *
 * THE SCENE. The exact three-row sequence from the field report (notice →
 * recovery card → give-up notice), first as it rendered before, then through
 * the real NoticeCard, plus the tone split (ℹ️ vs ⚠️ prefixes). RecoveryCard
 * and NoticeCard are the real components; their classes live under src/ so
 * Tailwind compiles them (capture/ is outside the scan glob). The BEFORE
 * replica is deliberately INLINE STYLES, not the old Tailwind recipe: once no
 * src/ file carries those classes any more, a class-based replica would
 * silently render unstyled and fake a "fixed" frame.
 *
 *   ?theme=dark|light   ?lang=zh-CN|en
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import RecoveryCard, { parseRecoveryMessage } from '../src/pages/chat/RecoveryCard'
import NoticeCard from '../src/pages/chat/NoticeCard'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const lang = params.get('lang') || 'zh-CN'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(lang)

/** Verbatim gateway-authored rows from the field report. */
const NOTICE_1 = 'ℹ️ The model returned nothing twice — auto-continuing once.'
const NOTICE_2 =
  'ℹ️ The model returned nothing this turn (it was retried and auto-continued ' +
  'automatically). Just send your message again to continue.'
const NOTICE_WARN = '⚠️ Queued message dropped: the session reset while it was waiting.'
const NOTICE_BLOCKED = '⛔ Tool call blocked by a Kiro Crew safety policy.'
const RECOVERY = parseRecoveryMessage(
  '[Empty response — automatic recovery]\n' +
    'Your previous turn produced no output (the model returned an empty ' +
    'response twice). Continue working on the pending request from the ' +
    'conversation above and respond now — do NOT restart from scratch and do ' +
    'NOT re-run steps or tools that already completed successfully.',
)!

/**
 * The old ChatPage notice branch, replicated with INLINE styles (computed from
 * the same theme tokens) rather than the retired class recipe — see the file
 * comment for why a class-based replica would be self-defeating evidence.
 */
function OldNotice({ content }: { content: string }) {
  return (
    <div
      style={{
        background: 'var(--card)',
        color: 'var(--muted)',
        fontSize: 13,
        lineHeight: '20px',
        padding: '8px 12px',
        borderRadius: 6,
        boxShadow: 'inset 0 0 0 1px var(--border)',
        alignSelf: 'center',
      }}
    >
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

function Episode({ unified }: { unified?: boolean }) {
  return (
    <div
      data-episode={unified ? 'after' : 'before'}
      style={{ display: 'flex', flexDirection: 'column', gap: 8 }}
    >
      {unified ? <NoticeCard content={NOTICE_1} /> : <OldNotice content={NOTICE_1} />}
      <RecoveryCard parsed={RECOVERY} />
      {unified ? <NoticeCard content={NOTICE_2} /> : <OldNotice content={NOTICE_2} />}
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
      <Label>BEFORE — notice box and RecoveryCard, two styles</Label>
      <Episode />
      <Label>AFTER — NoticeCard on RecoveryCard metrics</Label>
      <Episode unified />
      <Label>TONES — warn and blocked prefixes keep their severity</Label>
      <div data-episode="tones" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <NoticeCard content={NOTICE_WARN} />
        <NoticeCard content={NOTICE_BLOCKED} />
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Scene />)
