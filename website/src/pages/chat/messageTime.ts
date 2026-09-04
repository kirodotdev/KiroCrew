import { fmtDateFields, toDate } from '../../i18n/format'

/**
 * The per-message footer timestamp: "Aug 6, 05:52 PM".
 *
 * The year is ELIDED for messages from the current year, which is every message
 * a user reads in practice. It was noise on a line whose job is to say *when in
 * this conversation* a turn happened — the conversation itself never spans the
 * year boundary, so "2026" repeated under every single message carried no
 * information. This is the same rule `sessionOrder.ts` and `ChatSidebar.tsx`
 * already apply to session dates, so the chat surface is now internally
 * consistent rather than having the message footer be the one place that always
 * spells the year out.
 *
 * An older-year message DOES keep its year: dropping it there would make the
 * date actively wrong to read, not merely terse. Scrolled-back archives and
 * imported sessions are exactly where the year is load-bearing.
 *
 * `fmtDateFields`, not `toLocaleString`: the footer sits inside a translated UI
 * and must format in the APP's language, not the browser's.
 */
export function fmtMessageTime(ts: Date | string | number | null | undefined): string {
  const d = toDate(ts)
  if (!d) return ''
  const sameYear = d.getFullYear() === new Date().getFullYear()
  return fmtDateFields(d, {
    ...(sameYear ? {} : { year: 'numeric' }),
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/**
 * The full date for the footer's `title`, so eliding the year above never
 * DESTROYS the year — it moves it to hover. Always spelled out, including the
 * weekday, because a tooltip is read deliberately and has no width pressure.
 */
export function fmtMessageTimeFull(ts: Date | string | number | null | undefined): string {
  const d = toDate(ts)
  if (!d) return ''
  return fmtDateFields(d, {
    weekday: 'long',
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}
