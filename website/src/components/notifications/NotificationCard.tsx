import type { HTMLAttributes, MouseEvent, ReactNode } from 'react'
import { X } from 'lucide-react'

import Clickable from '../Clickable'
import type { Notification } from '../../types'
import {
  KIND_META, DEFAULT_META, notePriority, stripMd, fmtRelativeMinute,
  MAC_CARD_TINT_CLASS, MAC_CARD_BLUR_CLASS, MAC_CARD_SHADOW_CLASS, MAC_CARD_BORDER_CLASS,
  BANNER_CARD_TINT_CLASS, BANNER_CARD_SHADOW_CLASS, MAC_ACTION_BTN_CLASS,
} from './notifMeta'

/** Where the card floats: inside the bell sheet's own scrim (softer material)
 *  or over arbitrary page content as a banner (denser tint, deeper shadow).
 *  The material is the ONLY thing that differs between the two surfaces. */
export type NotificationCardElevation = 'popover' | 'banner'

export const CARD_MATERIAL: Record<NotificationCardElevation, string> = {
  popover: `${MAC_CARD_TINT_CLASS} ${MAC_CARD_BLUR_CLASS} ${MAC_CARD_SHADOW_CLASS} ${MAC_CARD_BORDER_CLASS}`,
  banner: `${BANNER_CARD_TINT_CLASS} ${MAC_CARD_BLUR_CLASS} ${BANNER_CARD_SHADOW_CLASS} ${MAC_CARD_BORDER_CLASS}`,
}

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
  elevation: NotificationCardElevation
  /** Full material override (the feed's silenced ghost and selected row keep
   *  their own border/tint); default is `CARD_MATERIAL[elevation]`. */
  material?: string
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
  /** Selected in a host that owns selection (the feed): keeps content undimmed. */
  active?: boolean
  /** Titles read muted for a silenced ghost row. */
  muted?: boolean
  /** Rendered under the actions (a failure notice the card must keep showing). */
  footer?: ReactNode
}

/**
 * The one notification card both mac-style surfaces render — the bell
 * popover's rows and the in-app banner — so a note never has two look-alike
 * renderings that drift apart. Layout is fixed: kind-tinted 26 px icon square,
 * title (13 px semibold, one line), body (12 px muted, two-line clamp, markdown
 * stripped and capped at 140 chars), relative time with the unread dot beneath
 * it, hover-reveal close, quiet capsule actions. Read (acked) and passive notes
 * dim; a critical note is signalled ONLY by its danger unread dot and the
 * approval kind's icon tint — never an edge or a label.
 *
 * Hosts wrap it: the feed adds its row anchor, stack deck and selection; the
 * banner adds motion and its own deck. Neither re-renders any of the body.
 */
export default function NotificationCard({
  n, elevation, material, onOpen, openLabel, onDismiss, dismissLabel, dismissTestId, dismissVisible = false,
  actions = [], actionsAlign = 'start', trailing, active = false, muted = false, footer, className = '', ...rest
}: NotificationCardProps) {
  const km = KIND_META[n.kind] || DEFAULT_META
  const prio = notePriority(n)
  const dim = muted ? 'opacity-50' : (n.acked && !active) || prio === 'passive' ? 'opacity-55' : ''
  const body = (
    <>
      <span className={`w-[26px] h-[26px] rounded-[8px] flex items-center justify-center shrink-0 text-[13px] ${km.color}`}>{km.icon}</span>
      <div className="flex-1 min-w-0">
        <div className={`text-[13px] font-semibold truncate leading-tight ${muted ? 'text-muted font-normal' : 'text-text-strong'}`}>{n.title}</div>
        <div className="text-[12px] text-muted mt-0.5 line-clamp-2 leading-snug">{stripMd(n.body || '').slice(0, 140)}</div>
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
  return (
    <div
      data-notification-card
      data-elevation={elevation}
      className={`notif-material group flex flex-col px-3 py-2.5 rounded-2xl transition-all ${material ?? CARD_MATERIAL[elevation]} ${className}`}
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
            className={`${dismissVisible ? 'opacity-60' : 'opacity-0 group-hover:opacity-50'} focus-visible:opacity-100 text-[11px] cursor-pointer hover:!opacity-100 hover:text-danger transition-opacity shrink-0`}
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
    </div>
  )
}
