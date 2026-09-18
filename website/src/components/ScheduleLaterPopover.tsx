import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Clock } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useDialogFocusTrap } from '../hooks/useDialogFocusTrap'
import { Btn, Input } from './ui'

/** Convert a local wall-clock picker value to epoch seconds. */
export function toFireTime(localValue: string, nowMs: number = Date.now()): number | null {
  if (!localValue) return null
  const ms = Date.parse(localValue)
  if (Number.isNaN(ms)) return null
  const secs = Math.floor(ms / 1000)
  return secs > Math.floor(nowMs / 1000) ? secs : null
}

/** Convert epoch seconds to the local wall-clock shape datetime-local expects. */
export function fireTimeLocal(epochSecs: number): string {
  const date = new Date(epochSecs * 1000)
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000)
  return local.toISOString().slice(0, 16)
}

/** Start on the next quarter-hour so the initial picker value is usable. */
export function defaultFireLocal(nowMs: number = Date.now()): string {
  const d = new Date(nowMs)
  d.setSeconds(0, 0)
  d.setMinutes(d.getMinutes() + (15 - (d.getMinutes() % 15) || 15))
  const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000)
  return local.toISOString().slice(0, 16)
}

export default function ScheduleLaterPopover({
  anchorRect,
  onSchedule,
  onClose,
  scheduling = false,
}: {
  anchorRect: DOMRect
  onSchedule: (atSecs: number) => void
  onClose: () => void
  scheduling?: boolean
}) {
  const { t } = useTranslation()
  const [local, setLocal] = useState(() => defaultFireLocal())
  const ref = useRef<HTMLDivElement | null>(null)

  // The element focused when this popover MOUNTED -- the control the user
  // activated to open it. Captured during the first render on purpose: render
  // runs before the focus-trap's entry effect moves focus onto the picker, so
  // this is the last moment the opener is still `document.activeElement`.
  // `undefined` is the "not yet captured" sentinel (same spelling as
  // `SlotPopover` in mochi's PackEditor.tsx and mochi's `ContextMenu`).
  const opener = useRef<Element | null | undefined>(undefined)
  if (opener.current === undefined) opener.current = document.activeElement

  // This is a role="dialog" holding a datetime-local field and one button, so
  // it takes the DIALOG keyboard contract, not the role="menu" one: focus in on
  // the picker, Escape dismisses, Tab/Shift-Tab cycle across BOTH controls. The
  // menu contract does not fit here: it leaves Escape to the host, so with Tab
  // contained a keyboard user has no way out, and it claims ArrowUp/ArrowDown
  // document-wide -- the keys a datetime-local field uses to step its segments.
  // `restoreFocus: false` because the hook's capture runs in a passive effect,
  // after the opener may already be gone, and its restore is unconditional; the
  // guarded restore below owns that half.
  useDialogFocusTrap(ref, onClose, { restoreFocus: false })

  // Hand focus back when the picker goes away. Every close path -- Escape,
  // confirming a time (the host unmounts this on `onSchedule`), a session switch
  // -- removes the element that holds focus, and the browser drops focus to
  // <body>, stranding a keyboard user at the top of the document. Restoring
  // from the unmount cleanup covers all of them at once; the guard restores
  // ONLY when focus is still inside the picker being destroyed, so a dismissal
  // that already routed focus elsewhere (an outside pointer on a focusable
  // control) is left alone. A layout cleanup, not a passive one: React
  // runs it while the node is still in the document and still holds focus. The
  // container is read at mount because React detaches host refs during
  // teardown. An opener the host unmounted in the same commit that opened this
  // picker (a menu row closing with its menu) is disconnected and cannot take
  // focus, so the host is expected to leave focus on a connected control.
  useLayoutEffect(() => {
    const container = ref.current
    return () => {
      const active = document.activeElement
      if (!container || !active || !container.contains(active)) return
      const target = opener.current
      if (target instanceof HTMLElement && target.isConnected) {
        target.focus({ preventScroll: true })
      }
    }
  }, [])

  useEffect(() => {
    const onDown = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) onClose()
    }
    document.addEventListener('pointerdown', onDown, true)
    return () => document.removeEventListener('pointerdown', onDown, true)
  }, [onClose])

  const fireTime = toFireTime(local)

  return createPortal(
    <div
      ref={ref}
      role="dialog"
      aria-label={t('components.chatInput.send_later')}
      data-testid="schedule-later-popover"
      className="fixed z-[60] w-[min(320px,calc(100vw-16px))] rounded-xl border border-border bg-bg-elevated p-2 shadow-xl"
      style={{
        left: Math.max(8, Math.min(anchorRect.left, window.innerWidth - 320 - 8)),
        bottom: window.innerHeight - anchorRect.top + 8,
      }}
    >
      <div className="flex min-w-0 flex-col gap-2 p-0.5">
        <label
          className="flex items-center gap-1.5 text-[12px] font-medium text-text"
          htmlFor="schedule-later-at"
        >
          <Clock className="h-3.5 w-3.5 shrink-0 text-muted lucide-inline" aria-hidden />
          {t('components.chatInput.send_at')}
        </label>
        <Input
          id="schedule-later-at"
          type="datetime-local"
          className="w-full min-w-0"
          aria-label={t('components.chatInput.send_at')}
          aria-invalid={fireTime === null}
          aria-describedby={fireTime === null ? 'schedule-later-at-error' : undefined}
          value={local}
          onChange={event => setLocal(event.target.value)}
          data-testid="schedule-later-at"
        />
        {fireTime === null ? (
          <p id="schedule-later-at-error" role="status" className="text-[11px] text-danger">
            {t('components.jobForm.pick_a_time_in_the_future')}
          </p>
        ) : null}
        <Btn
          primary
          onClick={() => {
            if (fireTime !== null) onSchedule(fireTime)
          }}
          disabled={fireTime === null || scheduling}
          data-testid="schedule-later-confirm"
        >
          {t('components.chatInput.schedule_message_confirm')}
        </Btn>
      </div>
    </div>,
    document.body,
  )
}
