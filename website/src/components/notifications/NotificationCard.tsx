import type { HTMLAttributes, MouseEvent, ReactNode } from 'react'
import { X } from 'lucide-react'

import Clickable from '../Clickable'
import Glass from '../Glass'
import type { Notification } from '../../types'
import { KIND_META, DEFAULT_META, notePriority, stripMd, fmtRelativeMinute, MAC_ACTION_BTN_CLASS } from './notifMeta'
import { CrewmateNoteFace, CrewmateNoteName, noteMember } from './CrewmateNoteFace'

/** Corner radius of every notification pane (the card, a deck shell, the
 *  feed's controls card): Tailwind's `rounded-2xl`, in px for `Glass`. */
export const CARD_RADIUS = 16

/** Semantic tint on an action's LABEL only — never a solid fill. */
export type CardActionTone = 'text' | 'accent' | 'ok' | 'danger' | 'muted'
const TONE_CLASS: Record<CardActionTone, string> = {
  text: 'text-text', accent: 'text-accent', ok: 'text-ok', danger: 'text-danger', muted: 'text-muted',
}

export interface NotificationCardAction {
  id: string
  label: ReactNode
  tone?: CardActionTone
  onClick: (e: MouseEvent<HTMLButtonElement>) => void
  /** Pushes this action to the row's far end (the feed's stack toggle). */
  trailing?: boolean
  'aria-expanded'?: boolean
}

export interface NotificationCardProps extends Omit<HTMLAttributes<HTMLDivElement>, 'title' | 'onClick'> {
  n: Notification
  /** Press on the card body. Omitted → decorative body (no control, no
   *  dismiss, no actions), for a deck card whose wrapper is the control. */
  onOpen?: () => void
  openLabel?: string
  onDismiss?: (e?: React.MouseEvent | React.KeyboardEvent) => void
  dismissLabel?: string
  dismissTestId?: string
  /** Show the close at rest instead of on hover -- a touch screen has no hover. */
  dismissVisible?: boolean
  actions?: NotificationCardAction[]
  /** Right-align the action row (the banner's ≤2 quiet capsules). */
  actionsAlign?: 'start' | 'end'
  /** Extra content under the timestamp (the feed's muted label / stack count). */
  trailing?: ReactNode
  /** Selected in a host that owns selection (the feed): the pane steps onto
   *  the accent tint (`glass-accent`) and its content stays undimmed. */
  active?: boolean
  /** A silenced row: the pane thins to the `glass-faded` tint step, its
   *  content dims and its title reads muted. */
  muted?: boolean
  /** Rendered under the actions (a failure notice the card must keep showing). */
  footer?: ReactNode
  /** Replaces the clamped excerpt. A card whose actions authorize a command
   *  must show the whole command: a two-line clamp turns `echo safe` +
   *  `rm -rf target` into a harmless-looking excerpt beside one-click Approve.
   *  The feed passes the full read-only approval render here; the banner,
   *  which offers only Review, keeps the excerpt. */
  body?: ReactNode
}

/**
 * The one notification card both mac-style surfaces render — the bell
 * popover's rows and the in-app banner — so a note never has two look-alike
 * renderings that drift apart. Layout is fixed: kind-tinted 26 px icon square
 * (the publishing crewmate's avatar instead, with its name above the title,
 * when the note carries `member` — see `CrewmateNoteFace`),
 * title (13 px semibold, one line), body (12 px muted, two-line clamp, markdown
 * stripped and capped at 140 chars -- unless the host passes `body`, which the
 * feed does for an approval so the command beside Approve/Reject is whole),
 * relative time with the unread dot beneath
 * it, hover-reveal close, quiet capsule actions. Read (acked) and passive notes
 * dim; a critical note is signalled ONLY by its danger unread dot and the
 * approval kind's icon tint — never an edge or a label.
 *
 * The card IS a `Glass` pane (`panel` recipe, `CARD_RADIUS`): the same
 * material the composer dock wears, so a note floating over the transcript and
 * the composer under it are one glass. It does not know which surface it is on
 * (the sheet's scrim or arbitrary page content): the pane reads correctly over
 * both, so there is nothing for an elevation to pick. Its state is a tint step
 * on the host — `glass-accent` for the feed's selected row, `glass-hover`
 * while a pressable card is hovered, `glass-faded` for a silenced row — never
 * a border, a fill or an `opacity` of its own.
 *
 * Hosts wrap it: the feed adds its row anchor, stack deck and selection; the
 * banner adds motion and its own deck. Neither re-renders any of the body.
 */
export default function NotificationCard({
  n, onOpen, openLabel, onDismiss, dismissLabel, dismissTestId, dismissVisible = false,
  actions = [], actionsAlign = 'start', trailing, active = false, muted = false, footer, body: bodyOverride, className = '', ...rest
}: NotificationCardProps) {
  const km = KIND_META[n.kind] || DEFAULT_META
  const prio = notePriority(n)
  const member = noteMember(n)
  const dim = muted ? 'opacity-50' : (n.acked && !active) || prio === 'passive' ? 'opacity-55' : ''
  const kindIcon = <span className={`w-[26px] h-[26px] rounded-[8px] flex items-center justify-center shrink-0 text-[13px] ${km.color}`}>{km.icon}</span>
  const body = (
    <>
      {member ? <CrewmateNoteFace member={member} size={26} fallback={kindIcon} /> : kindIcon}
      <div className="flex-1 min-w-0">
        {member && <CrewmateNoteName member={member} className="block text-[11px] text-muted truncate leading-tight mb-0.5" />}
        <div className={`text-[13px] font-semibold truncate leading-tight ${muted ? 'text-muted font-normal' : 'text-text-strong'}`}>{n.title}</div>
        {bodyOverride !== undefined
          ? bodyOverride
          : <div className="text-[12px] text-muted mt-0.5 line-clamp-2 leading-snug">{stripMd(n.body || '').slice(0, 140)}</div>}
      </div>
      <div className="flex flex-col items-end gap-0.5 shrink-0">
        <span className="text-[11px] text-muted">{fmtRelativeMinute(n.ts)}</span>
        {trailing}
        {!muted && !n.acked && (
          <span className={`w-1.5 h-1.5 rounded-full animate-dot-breathe ${prio === 'critical' ? 'bg-danger' : 'bg-accent'}`} data-priority={prio} />
        )}
      </div>
    </>
  )
  const interactive = !!onOpen
  // Recession is a tint step, never `opacity` on the host: opacity < 1 makes
  // the host a backdrop root and voids its own blur (see index.css).
  const tint = active ? 'glass-accent' : muted ? 'glass-faded' : interactive ? 'glass-hover' : ''
  return (
    <Glass
      variant="panel"
      radius={CARD_RADIUS}
      data-notification-card
      className={`notif-material glass-shadow group flex flex-col px-3 py-2.5 transition-all ${tint} ${className}`}
      {...rest}
    >
      <div className="flex items-start gap-2.5">
        {interactive ? (
          <Clickable
            onClick={onOpen}
            aria-label={openLabel}
            className={`flex items-start gap-2 flex-1 min-w-0 text-left cursor-pointer ${dim}`}
          >{body}</Clickable>
        ) : (
          <div className={`flex items-start gap-2 flex-1 min-w-0 text-left ${dim}`}>{body}</div>
        )}
        {interactive && onDismiss && (
          <Clickable
            aria-label={dismissLabel}
            data-testid={dismissTestId}
            className={`${dismissVisible ? 'opacity-60' : 'opacity-0 group-hover:opacity-50 [@media(hover:none)]:opacity-60'} focus-visible:opacity-100 text-[11px] cursor-pointer hover:!opacity-100 hover:text-danger transition-opacity shrink-0`}
            onClick={e => { e?.stopPropagation(); onDismiss(e) }}
          ><X className="lucide-inline" /></Clickable>
        )}
      </div>
      {interactive && actions.length > 0 && (
        <div className={`flex items-center gap-1.5 mt-1.5 flex-wrap pl-[36px] ${actionsAlign === 'end' ? 'justify-end' : ''}`}>
          {actions.map(a => (
            <button
              key={a.id}
              type="button"
              aria-expanded={a['aria-expanded']}
              className={`${MAC_ACTION_BTN_CLASS} ${TONE_CLASS[a.tone ?? 'text']} ${a.trailing ? 'ml-auto' : ''}`}
              onClick={e => { e.stopPropagation(); a.onClick(e) }}
            >{a.label}</button>
          ))}
        </div>
      )}
      {interactive && footer}
    </Glass>
  )
}
