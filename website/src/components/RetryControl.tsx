import { useEffect, useRef } from 'react'
import { RefreshCw } from 'lucide-react'
import { Btn } from './ui'

/**
 * The one Retry affordance the failure notices share.
 *
 * Spelled once because the four call sites had drifted into four copies of the same shell — the
 * spinner class, the `aria-busy` pair and the "<label>: <cause>" accessible name — which is the
 * divergence risk this PR already cites to justify `useBrowseDirs`.
 *
 * `label` stays a prop rather than a key inside here: each surface names the control in its own
 * catalog namespace, so hoisting the string would silently retire three translated keys.
 */
export function RetryControl({ busy, cause, label, controlRef, onRetry }: {
  /** True while THIS control's own retry is in flight. */
  busy: boolean
  /** The notice text, which becomes the accessible name's second half. */
  cause: string
  label: string
  /**
   * Optional handle for a parent that must reach the button itself — the @-menu's keyboard nav
   * gives Tab and Enter to this control when the list is empty, which needs the DOM node.
   */
  controlRef?: { current: HTMLButtonElement | null }
  /** Resolves to whether the read recovered; `false` keeps the user on the control. */
  onRetry: () => void | Promise<unknown>
}) {
  const own = useRef<HTMLButtonElement | null>(null)
  const ref = controlRef ?? own
  const wanted = useRef(false)

  useEffect(() => {
    if (busy || !wanted.current) return
    wanted.current = false
    ref.current?.focus()
  }, [busy, ref])

  return (
    <Btn
      ref={r => { ref.current = r }}
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
