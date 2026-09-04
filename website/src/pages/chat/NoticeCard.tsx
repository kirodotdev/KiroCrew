import { memo } from 'react'
import { Ban, Info, TriangleAlert } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'

export type NoticeTone = 'info' | 'warn' | 'blocked'

/**
 * Some gateway notices bake a severity emoji into the message text itself
 * (ℹ️ empty-response self-heal, ⚠️ dropped queue message / throttled model,
 * ⛔ policy-blocked sub-agent text), where it renders off-baseline beside the
 * copy; others (model fallback) carry no prefix at all. The prefix is parsed
 * into a tone here and stripped — the icon renders as a proper lucide glyph in
 * the leading slot instead — which also cleans up history rows written by
 * older gateways without needing a migration. Only these three glyphs are
 * recognized: an unknown leading glyph is CONTENT and stays in the text, so a
 * broader emoji class can never silently eat meaning.
 */
const LEAD_EMOJI_RE = /^\s*(\u2139|\u26A0|\u26D4)\uFE0F*\s*/u

export function parseNotice(content: string): { tone: NoticeTone; text: string } {
  const raw = content ?? ''
  const m = LEAD_EMOJI_RE.exec(raw)
  if (!m) return { tone: 'info', text: raw }
  const tone: NoticeTone = m[1] === '\u2139' ? 'info' : m[1] === '\u26A0' ? 'warn' : 'blocked'
  return { tone, text: raw.slice(m[0].length) }
}

/** Severity announced to assistive tech; the stripped emoji was previously the
 * only accessible signal, so a visually-hidden label replaces it. Info stays
 * silent — a routine notice needs no severity call-out. */
function srSeverity(tone: NoticeTone): string {
  if (tone === 'warn') return i18nT('pages.chat.noticeCard.warning')
  if (tone === 'blocked') return i18nT('pages.chat.noticeCard.blocked')
  return ''
}

/**
 * Soft notice row ("the gateway did something routine on your behalf" — an
 * empty-response self-heal, a model fallback, a dropped queued message).
 *
 * Shares RecoveryCard's visual grammar — the same ring, background, radius,
 * padding step, 13px/leading-5 type and 13px lucide icon in a gap-2 leading
 * slot — because on the main chat transcript the two rows land stacked in the
 * same episode (an empty-response episode emits both) and must read as one
 * family, not two unrelated boxes. The severity split extends RecoveryCard's:
 * info keeps the muted glyph, warn gets the warning triangle, and blocked (a
 * policy denial) gets its own Ban glyph in the danger color, so a security
 * block is never dressed as a routine warning. Distinct glyph shapes also
 * carry the severity under forced-colors, where the tint does not survive.
 *
 * Unlike RecoveryCard's single-line truncated header, notice copy wraps, so
 * the row top-aligns and the icon is nudged onto the first line's vertical
 * center. The nudge is calc((1.25rem − 1em) / 2): leading-5 is a rem line box
 * while the glyph is 1em of the fixed 13px type, so a px constant would drift
 * under a non-16px root font-size.
 */
export default memo(function NoticeCard({ content }: { content: string }) {
  // Language-generation subscription: this memo() boundary renders i18nT()
  // strings, so a language switch must invalidate it.
  useLanguageGeneration()
  const { tone, text } = parseNotice(content)
  const Icon = tone === 'blocked' ? Ban : tone === 'warn' ? TriangleAlert : Info
  const severity = srSeverity(tone)
  return (
    <div
      className="self-center w-full max-w-full min-w-0 rounded-md ring-1 ring-inset forced-colors:border ring-border bg-card text-muted animate-scale-in"
      data-testid="notice-card"
      data-tone={tone}
    >
      <div className="flex items-start gap-2 px-3 py-2 min-w-0 text-[13px] leading-5">
        <Icon
          size={13}
          className={`lucide-inline shrink-0 mt-[calc((1.25rem-1em)/2)] ${
            tone === 'blocked' ? 'text-danger' : tone === 'warn' ? 'text-warn' : ''
          }`}
          aria-hidden="true"
        />
        <span className="min-w-0 break-words">
          {severity && <span className="sr-only">{severity} </span>}
          {text}
        </span>
      </div>
    </div>
  )
})
