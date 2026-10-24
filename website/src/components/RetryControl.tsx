import { useEffect, useRef } from 'react'
import { RefreshCw } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { Btn } from './ui'

/**
 * The Retry affordance a failure notice renders: a button whose accessible name is
 * "Retry: <cause>", paired with `aria-busy` while its own retry is in flight.
 */
export function RetryControl({ busy, cause, controlRef, onRetry }: {
  /** True while THIS control's own retry is in flight. */
  busy: boolean
  /** The notice text, which becomes the accessible name's second half. */
  cause: string
  /**
   * Handle for the parent that must reach the button itself — the @-menu's keyboard nav
   * gives Tab and Enter to this control when the list is empty, which needs the DOM node.
   */
  controlRef: { current: HTMLButtonElement | null }
  /** Resolves to whether the read recovered; `false` keeps the user on the control. */
  onRetry: () => void | Promise<unknown>
}) {
  const wanted = useRef(false)
  const label = i18nT('components.filePickerMenu.retry')

  useEffect(() => {
    if (busy || !wanted.current) return
    wanted.current = false
    controlRef.current?.focus()
  }, [busy, controlRef])

  return (
    <Btn
      ref={r => { controlRef.current = r }}
      type="button"
      // Keeps the field behind the notice focused: without it the mousedown blurs the input
      // before the click lands, so a successful retry has nowhere to hand focus back to.
      onMouseDown={e => e.preventDefault()}
      onClick={() => {
        // A disabled button cannot hold focus, so a failed attempt would otherwise cost a
        // keyboard user a Tab per retry. Unconditional: every call site wanted it.
        wanted.current = true
        void onRetry()
      }}
      disabled={busy}
      aria-busy={busy}
      aria-label={`${label}: ${cause}`}
      className="shrink-0 text-[11px] px-1.5 py-0.5 rounded"
    >
      <RefreshCw
        size={10}
        className={busy ? 'animate-spin inline-block mr-1' : 'invisible inline-block mr-1'}
      />
      {label}
    </Btn>
  )
}
