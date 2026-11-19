/** "Action for you · <newest title>": the status a session shows while the
 *  agent has an ask pinned, in the `--aim` ink that no other session state
 *  uses. Never shown over a live question or approval (`actionForYou`). */
import { Hand } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { actionForYou } from '../../lib/waitingOnYou'
import type { ChatSlot } from '../../types'

type StatusSlot = Pick<ChatSlot, 'waiting_on_you' | 'pending_approval' | 'needs_input'>

/** The status sentence, or '' when the slot shows no ask. */
export function actionForYouLabel(slot: StatusSlot | null | undefined): string {
  const ask = actionForYou(slot)
  return ask ? i18nT('components.waitingOnYou.status_line', { title: ask.title }) : ''
}

/** The grid pane header's pill. */
export function ActionForYouPill({ slot }: { slot: StatusSlot | null | undefined }) {
  const label = actionForYouLabel(slot)
  if (!label) return null
  return (
    <span data-testid="action-for-you-status" title={label}
      className="shrink min-w-0 max-w-[45%] inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px] font-semibold text-aim bg-aim-subtle">
      <Hand size={10} className="shrink-0" aria-hidden />
      <span className="truncate">{label}</span>
    </span>
  )
}

/** The sidebar row's status line, in the row's own line and icon classes. */
export function ActionForYouRowLine({ slot, lineCls, iconPx }: { slot: StatusSlot; lineCls: string; iconPx: number }) {
  const label = actionForYouLabel(slot)
  return (
    <div className={lineCls} title={label} data-testid="row-action-for-you">
      <Hand size={iconPx} className="shrink-0" style={{ color: 'var(--aim)' }} aria-hidden />
      <span className="truncate font-medium" style={{ color: 'var(--aim)' }}>{label}</span>
    </div>
  )
}
