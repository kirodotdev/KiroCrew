import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Clock } from 'lucide-react'
import { useMenuKeyboard } from '../hooks/useMenuKeyboard'
import { i18nT } from '../i18n/t'

/** Turn a `datetime-local` value into epoch SECONDS, or null if unusable.
 *
 * The input has no zone, so it is read as the viewer's own wall clock — which is
 * what the picker means — and sent as an absolute instant. A time already past
 * would fire the moment it was saved, so it is rejected here rather than accepted
 * and surprising the user. */
export function toFireTime(localValue: string, nowMs: number = Date.now()): number | null {
  if (!localValue) return null
  const ms = Date.parse(localValue)
  if (Number.isNaN(ms)) return null
  const secs = Math.floor(ms / 1000)
  return secs > Math.floor(nowMs / 1000) ? secs : null
}

/** A default one minute into the next quarter hour, as `datetime-local` text.
 *
 * A picker that opens on "now" is always already invalid by the time it is read,
 * so the initial value is a time the user can accept without editing. */
export function defaultFireLocal(nowMs: number = Date.now()): string {
  const d = new Date(nowMs)
  d.setSeconds(0, 0)
  d.setMinutes(d.getMinutes() + (15 - (d.getMinutes() % 15)) || 15)
  const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000)
  return local.toISOString().slice(0, 16)
}

/**
 * Pick a time to send the composer's draft later.
 *
 * Opened from the composer's plus menu rather than a caret on Send. A caret would
 * be the more discoverable shape (it is what Slack does), but the idle composer's
 * action row already carries mic + Optimize + Send, and `max-two-buttons-per-row`
 * caps a row at two peers and explicitly rejects widening or wrapping as the
 * remedy. The plus menu is a separate visual group, so a row there adds nothing to
 * the capped row and relocates none of the controls already in it.
 *
 * Anchored like the plus menu itself (bottom edge above the trigger, left clamped
 * into the viewport) so the two panels open in the same place.
 */
export default function ScheduleLaterPopover({
  anchorRect,
  onSchedule,
  onClose,
  scheduling,
}: {
  anchorRect: DOMRect
  /** Called with an absolute epoch-seconds fire time. */
  onSchedule: (atSecs: number) => void
  onClose: () => void
  scheduling?: boolean
}) {
  const [local, setLocal] = useState(() => defaultFireLocal())
  const ref = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)

  // Escape and focus containment, without `focusFirstOnOpen`: the first control
  // here is a time input the user needs the caret in, not an item to arrow
  // between, so focus is placed explicitly below.
  useMenuKeyboard({ enabled: true, containerRef: ref })

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  // Dismiss on an outside pointer press. The panel is portaled, so a press inside
  // it is not a DOM descendant of the trigger and has to be excluded explicitly.
  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (!ref.current?.contains(e.target as Node)) onClose()
    }
    document.addEventListener('pointerdown', onDown, true)
    return () => document.removeEventListener('pointerdown', onDown, true)
  }, [onClose])

  const fireTime = toFireTime(local)

  return createPortal(
    <div
      ref={ref}
      role="dialog"
      aria-label={i18nT('components.chatInput.send_later')}
      data-testid="schedule-later-popover"
      className="fixed z-[60] w-[260px] rounded-xl border border-border bg-bg-elevated p-2 shadow-xl"
      style={{
        left: Math.max(8, Math.min(anchorRect.left, window.innerWidth - 260 - 8)),
        bottom: window.innerHeight - anchorRect.top + 8,
      }}
    >
      <div className="flex flex-col gap-2 p-0.5">
        <label className="flex items-center gap-1.5 text-[12px] font-medium text-text" htmlFor="schedule-later-at">
          <Clock size={14} className="w-4 shrink-0 text-muted lucide-inline" />
          {i18nT('components.chatInput.send_later')}
        </label>
        <input
          ref={inputRef}
          id="schedule-later-at"
          type="datetime-local"
          // Labelled twice on purpose: the visible `<label htmlFor>` is what a
          // sighted user reads, and `aria-label` is what
          // `jsx-a11y/control-has-associated-label` requires on the control itself
          // (it does not follow the htmlFor/id pairing for this rule).
          aria-label={i18nT('components.chatInput.send_later')}
          className="rounded-md border border-border bg-bg px-2 py-1 text-[13px] text-text"
          value={local}
          onChange={e => setLocal(e.target.value)}
          data-testid="schedule-later-at"
        />
        <button
          type="button"
          className="rounded-md bg-accent px-2 py-1 text-[13px] text-accent-fg border-none cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
          onClick={() => {
            if (fireTime === null) return
            onSchedule(fireTime)
          }}
          // Disabled rather than error-on-submit: the only failure this picker can
          // produce is a past or empty time, and both are visible in the control
          // the user is already looking at.
          disabled={fireTime === null || !!scheduling}
          data-testid="schedule-later-confirm"
        >
          {i18nT('components.chatInput.schedule')}
        </button>
      </div>
    </div>,
    document.body,
  )
}
